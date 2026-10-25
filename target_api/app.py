"""A small demo store API for API Sentinel to test.

Set ``TARGET_API_FAULTS=1`` to inject two deliberate problems:
  * ``GET /items/{item_id}`` sleeps an extra 200 ms (a latency regression), and
  * ``POST /items`` crashes with a 500 when ``description`` is null or omitted
    (an unhandled-null bug - the schema says the field is optional).

Run: ``uvicorn target_api.app:app --port 9000``
"""

from __future__ import annotations

import asyncio
import os
import threading
from enum import StrEnum
from typing import Annotated

from fastapi import FastAPI, HTTPException, Path, Query, status
from pydantic import BaseModel, Field

SLOW_DELAY_S = 0.2
MAX_STORED_ITEMS = 10_000


def faults_enabled() -> bool:
    """Read the fault switch at request time so it can be flipped in tests."""
    return os.getenv("TARGET_API_FAULTS", "").strip().lower() in {"1", "true", "yes", "on"}


class Category(StrEnum):
    """Item category."""

    BOOKS = "books"
    ELECTRONICS = "electronics"
    TOYS = "toys"


class ItemIn(BaseModel):
    """Payload to create an item."""

    name: str = Field(min_length=1, max_length=50)
    price: float = Field(gt=0, le=10_000)
    quantity: int = Field(ge=0, le=1_000)
    category: Category
    description: str | None = Field(default=None, max_length=500)


class Item(ItemIn):
    """A stored item."""

    id: int
    summary: str


app = FastAPI(title="Demo Store API", version="1.0.0")

_lock = threading.Lock()
_items: dict[int, Item] = {}


def _seed() -> None:
    samples = [
        ("Dune", 9.99, 12, Category.BOOKS, "Sci-fi classic"),
        ("Headphones", 59.0, 30, Category.ELECTRONICS, "Over-ear, wired"),
        ("Robot kit", 120.5, 4, Category.TOYS, None),
        ("Keyboard", 45.0, 18, Category.ELECTRONICS, "Mechanical"),
        ("Atlas", 25.0, 7, Category.BOOKS, "World atlas"),
    ]
    for i, (name, price, qty, cat, desc) in enumerate(samples, start=1):
        _items[i] = Item(
            id=i,
            name=name,
            price=price,
            quantity=qty,
            category=cat,
            description=desc,
            summary=(desc or "").strip(),
        )


_seed()


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness check."""
    return {"status": "ok"}


@app.get("/items")
def list_items(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    category: Category | None = None,
) -> list[Item]:
    """List items, optionally filtered by category."""
    with _lock:
        items = [i for i in _items.values() if category is None or i.category == category]
    return items[:limit]


@app.get("/items/{item_id}")
async def get_item(item_id: Annotated[int, Path(ge=1)]) -> Item:
    """Fetch one item by id."""
    if faults_enabled():
        await asyncio.sleep(SLOW_DELAY_S)  # deliberate latency regression
    with _lock:
        item = _items.get(item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "item not found")
    return item


@app.post("/items", status_code=status.HTTP_201_CREATED)
def create_item(payload: ItemIn) -> Item:
    """Create an item."""
    if faults_enabled():
        summary = payload.description.strip()[:100]  # type: ignore[union-attr]  # deliberate null bug
    else:
        summary = (payload.description or "").strip()[:100]
    with _lock:
        item_id = max(_items, default=0) + 1
        item = Item(id=item_id, summary=summary, **payload.model_dump())
        if len(_items) < MAX_STORED_ITEMS:
            _items[item_id] = item
    return item


@app.get("/search")
def search(
    q: Annotated[str, Query(min_length=1, max_length=100)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[Item]:
    """Case-insensitive substring search over item names."""
    needle = q.lower()
    with _lock:
        hits = [i for i in _items.values() if needle in i.name.lower()]
    return hits[:limit]
