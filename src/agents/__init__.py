"""Agents package -- five-layer agent system for Atlas outreach.

Layer 1: ScoutAgent         -- intelligence gathering (zero outreach)
Layer 2: ExecutorAgent   -- trust-building and soft outreach (core)
Layer 3: ContentSeederAgent -- high-quality original content production
Layer 5: EventAgent   -- event-driven signal handling
Base:    BaseAgent          -- abstract foundation for all agents
"""

from src.agents.base import BaseAgent
from src.agents.scout.agent import ScoutAgent
from src.agents.executor.agent import ExecutorAgent
from src.agents.content.agent import ContentSeederAgent
from src.agents.events.agent import EventAgent

__all__ = [
    "BaseAgent",
    "ScoutAgent",
    "ExecutorAgent",
    "ContentSeederAgent",
    "EventAgent",
]
