# main.py
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, validator
from typing import Optional, List, Literal
from decimal import Decimal, ROUND_DOWN, getcontext
from datetime import datetime, timezone
from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime,
    Numeric, Text
)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import SQLAlchemyError
from fastapi.responses import JSONResponse

# Set decimal precision to something safe; we'll quantize to 2 dp for kWh
getcontext().prec = 28

Base = declarative_base()

# --- Database models ---
class Meter(Base):
    __tablename__ = "meter"
    id = Column(Integer, primary_key=True, autoincrement=True)
    # store current kWh
    current_kwh = Column(Numeric(12, 2), nullable=False)

class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    type = Column(String(16), nullable=False)  # 'set' or 'topup'
    prev_kwh = Column(Numeric(12, 2), nullable=False)
    new_kwh = Column(Numeric(12, 2), nullable=False)
    delta_kwh = Column(Numeric(12, 2), nullable=False)
    rand_amount = Column(Numeric(14, 2), nullable=True)  # R amount for topups
    note = Column(Text, nullable=True)
    timestamp = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

# --- Database setup ---
DATABASE_URL = "sqlite:///./meter.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Prepaid Electricity Meter API")

# Ensure there is a meter row (single row app)
def get_or_create_meter(db):
    meter = db.query(Meter).first()
    if not meter:
        meter = Meter(current_kwh=Decimal("0.00"))
        db.add(meter)
        db.commit()
        db.refresh(meter)
    return meter

# Helper for quantizing decimals to 2 decimal places
def quantize_kwh(value) -> Decimal:
    if not isinstance(value, Decimal):
        value = Decimal(value)
    return value.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

def quantize_rand(value) -> Decimal:
    if value is None:
        return None
    if not isinstance(value, Decimal):
        value = Decimal(value)
    return value.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

# --- Pydantic models ---
class SetMeterRequest(BaseModel):
    kwh: Decimal = Field(..., description="Absolute value to set current meter to (kWh)")
    note: Optional[str] = None

    @validator("kwh")
    def two_decimal_places(cls, v):
        try:
            return quantize_kwh(v)
        except Exception:
            raise ValueError("kwh must be a number")

class TopUpRequest(BaseModel):
    # Either rand_amount + price_per_kwh OR kwh must be supplied.
    rand_amount: Optional[Decimal] = Field(None, description="Amount in South African Rands (R)")
    price_per_kwh: Optional[Decimal] = Field(None, description="Price per kWh in R; required if rand_amount provided")
    kwh: Optional[Decimal] = Field(None, description="kWh to add directly (if you prefer to top-up by kWh)")
    note: Optional[str] = None

    @validator("rand_amount", "price_per_kwh", "kwh", pre=True)
    def accept_strings(cls, v):
        if v is None:
            return None
        return Decimal(str(v))

    @validator("kwh")
    def quantize_kwh_field(cls, v):
        if v is None:
            return None
        return quantize_kwh(v)

    @validator("rand_amount")
    def quantize_rand_field(cls, v):
        if v is None:
            return None
        return quantize_rand(v)

    @validator("price_per_kwh")
    def quantize_price(cls, v):
        if v is None:
            return None
        return quantize_rand(v)

    def compute_delta_kwh(self) -> Decimal:
        """
        Resolve the kWh delta either from kwh field or by computing rand/price.
        """
        if self.kwh is not None:
            return quantize_kwh(self.kwh)
        if self.rand_amount is not None:
            if not self.price_per_kwh or self.price_per_kwh == Decimal("0"):
                raise ValueError("price_per_kwh must be provided and non-zero when using rand_amount")
            delta = (self.rand_amount / self.price_per_kwh)
            return quantize_kwh(delta)
        raise ValueError("Either kwh or rand_amount (+ price_per_kwh) must be provided")

class TransactionOut(BaseModel):
    id: int
    type: str
    prev_kwh: Decimal
    new_kwh: Decimal
    delta_kwh: Decimal
    rand_amount: Optional[Decimal]
    note: Optional[str]
    timestamp: datetime

    class Config:
        orm_mode = True

# --- API endpoints ---

@app.on_event("startup")
def startup_event():
    db = SessionLocal()
    try:
        get_or_create_meter(db)
    finally:
        db.close()

@app.get("/meter", response_model=dict)
def get_meter():
    """Return current meter value (kWh)"""
    db = SessionLocal()
    try:
        meter = get_or_create_meter(db)
        return {"current_kwh": quantize_kwh(meter.current_kwh)}
    finally:
        db.close()

@app.post("/meter/set", response_model=TransactionOut)
def set_meter(payload: SetMeterRequest):
    """
    Set the current kWh to an absolute value (overwrite).
    Records a transaction of type 'set'.
    """
    db = SessionLocal()
    try:
        meter = get_or_create_meter(db)
        prev = quantize_kwh(meter.current_kwh)
        new_kwh = quantize_kwh(payload.kwh)
        delta = quantize_kwh(new_kwh - prev)
        # update
        meter.current_kwh = new_kwh
        tx = Transaction(
            type="set",
            prev_kwh=prev,
            new_kwh=new_kwh,
            delta_kwh=delta,
            rand_amount=None,
            note=payload.note,
            timestamp=datetime.now(timezone.utc)
        )
        db.add(tx)
        db.commit()
        db.refresh(tx)
        return tx
    except SQLAlchemyError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/meter/topup", response_model=TransactionOut)
def topup_meter(payload: TopUpRequest):
    """
    Top-up the meter:
      - Provide `kwh` to add directly, OR
      - Provide `rand_amount` and `price_per_kwh` to compute kWh to add.
    Records a transaction of type 'topup'.
    """
    db = SessionLocal()
    try:
        delta_kwh = payload.compute_delta_kwh()
        if delta_kwh == Decimal("0.00"):
            raise HTTPException(status_code=400, detail="Computed delta kWh is 0.00")
        meter = get_or_create_meter(db)
        prev = quantize_kwh(meter.current_kwh)
        new_kwh = quantize_kwh(prev + delta_kwh)
        rand_amt = quantize_rand(payload.rand_amount) if payload.rand_amount is not None else None

        meter.current_kwh = new_kwh
        tx = Transaction(
            type="topup",
            prev_kwh=prev,
            new_kwh=new_kwh,
            delta_kwh=delta_kwh,
            rand_amount=rand_amt,
            note=payload.note,
            timestamp=datetime.now(timezone.utc)
        )
        db.add(tx)
        db.commit()
        db.refresh(tx)
        return tx
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SQLAlchemyError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.get("/transactions", response_model=List[TransactionOut])
def list_transactions(limit: int = Query(200, ge=1, le=5000), offset: int = 0):
    """Return transactions (most recent first)"""
    db = SessionLocal()
    try:
        q = db.query(Transaction).order_by(Transaction.timestamp.desc()).offset(offset).limit(limit).all()
        return q
    finally:
        db.close()

# Timeseries aggregation suitable for visualization
@app.get("/timeseries")
def timeseries(
    from_ts: Optional[datetime] = Query(None, description="ISO datetime, inclusive"),
    to_ts: Optional[datetime] = Query(None, description="ISO datetime, inclusive"),
    granularity: Literal["hourly", "daily"] = "daily"
):
    """
    Returns aggregated timeseries of meter values over time.

    Response: list of { period_start: ISO8601, kwh: Decimal(last known kWh in period), rand_spent: Decimal(sum rand in that period) }

    The timeseries reports the last known meter value in each period and sum of rand_amount for topups in that period.
    """
    db = SessionLocal()
    try:
        q = db.query(Transaction).order_by(Transaction.timestamp.asc())
        if from_ts:
            q = q.filter(Transaction.timestamp >= from_ts)
        if to_ts:
            q = q.filter(Transaction.timestamp <= to_ts)

        rows = q.all()

        # bucket by period
        buckets = {}
        def bucket_key(ts: datetime):
            ts = ts.astimezone(timezone.utc)
            if granularity == "hourly":
                return ts.replace(minute=0, second=0, microsecond=0)
            else:  # daily
                return ts.replace(hour=0, minute=0, second=0, microsecond=0)

        for r in rows:
            b = bucket_key(r.timestamp)
            entry = buckets.get(b)
            if not entry:
                entry = {"period_start": b, "kwh": None, "rand_spent": Decimal("0.00")}
                buckets[b] = entry
            # we want the last known kwh in the period -> since rows are asc, just overwrite
            entry["kwh"] = quantize_kwh(r.new_kwh)
            if r.rand_amount is not None:
                entry["rand_spent"] = quantize_rand(entry["rand_spent"] + quantize_rand(r.rand_amount))

        # convert to sorted list
        out = []
        for k in sorted(buckets.keys()):
            entry = buckets[k]
            out.append({
                "period_start": entry["period_start"].isoformat(),
                "kwh": entry["kwh"],
                "rand_spent": entry["rand_spent"]
            })

        return {"granularity": granularity, "series": out}
    finally:
        db.close()

@app.get("/summary")
def summary():
    """
    Basic quick summary: current kWh, total topups (R), total kWh added from topups, number of transactions.
    """
    db = SessionLocal()
    try:
        meter = get_or_create_meter(db)
        total_topups = db.query(Transaction).filter(Transaction.type == "topup").all()
        total_r = sum([t.rand_amount or Decimal("0.00") for t in total_topups], Decimal("0.00"))
        total_kwh_added = sum([t.delta_kwh for t in total_topups], Decimal("0.00"))
        total_tx = db.query(Transaction).count()
        return {
            "current_kwh": quantize_kwh(meter.current_kwh),
            "total_topups_rand": quantize_rand(total_r),
            "total_topups_kwh": quantize_kwh(total_kwh_added),
            "total_transactions": total_tx
        }
    finally:
        db.close()

# Simple health
@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}

# Generic exception handler for Decimal -> JSON conversions
@app.exception_handler(HTTPException)
def http_exception_handler(request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc.detail)})
