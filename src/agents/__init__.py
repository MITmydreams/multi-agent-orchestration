"""Agents package -- five-layer agent system for The Button promotion.

Layer 1: ScoutAgent         -- intelligence gathering (zero promotion)
Layer 2: InfiltratorAgent   -- trust-building and soft promotion (core)
Layer 3: ContentSeederAgent -- high-quality original content production
Layer 5: ViralEngineAgent   -- event-driven viral propagation
Base:    BaseAgent          -- abstract foundation for all agents
"""

from src.agents.base import BaseAgent
from src.agents.scout.agent import ScoutAgent
from src.agents.infiltrator.agent import InfiltratorAgent
from src.agents.content.agent import ContentSeederAgent
from src.agents.viral.agent import ViralEngineAgent

__all__ = [
    "BaseAgent",
    "ScoutAgent",
    "InfiltratorAgent",
    "ContentSeederAgent",
    "ViralEngineAgent",
]
