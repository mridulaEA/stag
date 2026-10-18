"""STAG crews: declarative role + skill + workflow compositions run on the TechOps backbone."""

from stag.crews.loader import (
    CREWS_DIR,
    discover_crews,
    load_crew,
    load_scenario,
    validate_all,
    validate_crew,
)
from stag.crews.runner import (
    CrewRunner,
    CrewRunReport,
    DeterministicExecutor,
    RoleExecutor,
    run_crew,
)
from stag.crews.schema import Crew, Role, Scenario, Step, StepAction

__all__ = [
    "CREWS_DIR",
    "Crew",
    "CrewRunReport",
    "CrewRunner",
    "DeterministicExecutor",
    "Role",
    "RoleExecutor",
    "Scenario",
    "Step",
    "StepAction",
    "discover_crews",
    "load_crew",
    "load_scenario",
    "run_crew",
    "validate_all",
    "validate_crew",
]
