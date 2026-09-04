from pydantic import BaseModel, Field


class PriceQueryGroupWrite(BaseModel):
    name: str = Field(min_length=1, max_length=64)
