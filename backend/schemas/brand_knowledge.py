from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class NamedItem(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)


class ProjectClassification(BaseModel):
    product_id: str | None = None


class KnowledgeLibraryCreate(NamedItem):
    pass


class ProjectKnowledgeBindingUpdate(BaseModel):
    product_id: str | None = None


class PageCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(default="", max_length=100_000)
    kind: Literal["folder", "page"] = "page"
    parent_id: str | None = None
    product_id: str | None = None


class PageUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(max_length=100_000)
    status: Literal["draft", "published", "archived"]
    revision: int = Field(ge=1)


class KnowledgeQuestion(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=1000)
    product_id: str | None = None
