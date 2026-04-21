from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import create_db_and_tables
from app.routes import health, imports, matching, receipts, reports, reviews, statements, telegram, transactions


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    yield


app = FastAPI(title="Expense Reporting App", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(statements.router, prefix="/statements", tags=["statements"])
app.include_router(transactions.router, prefix="/transactions", tags=["transactions"])
app.include_router(receipts.router, prefix="/receipts", tags=["receipts"])
app.include_router(imports.router, prefix="/imports", tags=["imports"])
app.include_router(matching.router, prefix="/matching", tags=["matching"])
app.include_router(reviews.router, prefix="/reviews", tags=["reviews"])
app.include_router(telegram.router, prefix="/telegram", tags=["telegram"])
app.include_router(reports.router, prefix="/reports", tags=["reports"])
