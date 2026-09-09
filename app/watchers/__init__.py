from .city_subsidy import CitySubsidyWatcher
from .campus_jobs import CampusJobsWatcher
from .subsidy_watch import SubsidyWatcher
from .xuandiao_watch import XuandiaoWatcher

__all__ = ["CampusJobsWatcher", "CitySubsidyWatcher", "SubsidyWatcher", "XuandiaoWatcher", "XhsRuleWatcher"]


def __getattr__(name: str):
    # Keep the package-level import compatible without importing the module
    # before `python -m app.watchers.xhs_rule_watch` executes it.
    if name == "XhsRuleWatcher":
        from .xhs_rule_watch import XhsRuleWatcher

        return XhsRuleWatcher
    raise AttributeError(name)
