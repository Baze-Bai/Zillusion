"""Domain models — Pydantic schemas that flow through the entire pipeline."""

from src.models.candidates import ProcessedCandidate, RawCandidate, TypeClassifiedCandidate
from src.models.data_source import AccessLevel, DataSource, DataSourceType
from src.models.page_tree import DataPageNode, DataPageTree
from src.models.report import CriticOutput, FinalReport
from src.models.requirement import StructuredRequirement
from src.models.routing import ActivatedWorkerSet
from src.models.scores import SourceScores
from src.models.specs import APISpec, EmbeddedSpec, FileSpec

__all__ = [
    "DataSource",
    "DataSourceType",
    "AccessLevel",
    "APISpec",
    "FileSpec",
    "EmbeddedSpec",
    "DataPageTree",
    "DataPageNode",
    "StructuredRequirement",
    "SourceScores",
    "RawCandidate",
    "TypeClassifiedCandidate",
    "ProcessedCandidate",
    "FinalReport",
    "CriticOutput",
    "ActivatedWorkerSet",
]
