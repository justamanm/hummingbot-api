from pydantic import BaseModel, Field


class PriceQueryGroupWrite(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    normal_check_interval: int = Field(default=4, ge=1, le=15)
    buy_trailing_check_interval: int = Field(default=1, ge=1, le=15)
