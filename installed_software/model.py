from dataclasses import dataclass, field
from typing import Any


@dataclass
class Application:
    id: str
    name: str
    installation_type: str
    package_name: str = ""
    version: str = ""
    description: str = ""
    icon: str = "application-x-executable"
    architecture: str = ""
    size: int | None = None
    install_location: str = ""
    publisher: str = ""
    source: str = ""
    category: str = ""
    desktop_files: list[str] = field(default_factory=list)
    desktop_application: bool = False
    system_package: bool = False
    can_uninstall: bool = False
    can_update: bool = False
    update_version: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def search_text(self) -> str:
        return " ".join(
            (
                self.name,
                self.package_name,
                self.description,
                self.publisher,
                self.installation_type,
                self.source,
            )
        ).casefold()


@dataclass
class DiscoveryResult:
    applications: list[Application] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)


@dataclass
class Plan:
    application_id: str
    title: str
    explanation: str
    argv: list[str] = field(default_factory=list)
    local_delete: dict[str, Any] | None = None


def human_size(value: int | None) -> str:
    if value is None:
        return "Unknown"
    number = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if number < 1024 or unit == "TiB":
            return f"{number:.1f} {unit}"
        number /= 1024
    return "Unknown"


def fuzzy_match(query: str, application: Application) -> bool:
    """Substring matching first; bounded subsequence matching for names."""
    terms = query.casefold().split()
    text = application.search_text()
    short = f"{application.name} {application.package_name}".casefold()

    def matches(term: str) -> bool:
        if term in text:
            return True
        if len(term) < 3:
            return False
        iterator = iter(short)
        return all(any(char == candidate for candidate in iterator) for char in term)

    return all(matches(term) for term in terms)
