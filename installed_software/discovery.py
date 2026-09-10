import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from .backends import AppImageBackend, AptBackend, FlatpakBackend, SnapBackend
from .desktop import dpkg_owners, merge_applications, scan_desktops
from .model import DiscoveryResult
from .process import Runner

LOG = logging.getLogger(__name__)


class DiscoveryService:
    def __init__(self, runner: Runner | None = None):
        self.runner = runner or Runner()
        self.backends = [
            AptBackend(self.runner),
            FlatpakBackend(self.runner),
            SnapBackend(self.runner),
            AppImageBackend(self.runner),
        ]

    def discover(self) -> DiscoveryResult:
        result = DiscoveryResult()
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {}
            for backend in self.backends:
                LOG.info("discovery_started backend=%s", backend.key)
                futures[pool.submit(backend.discover)] = backend.key
            desktop_future = pool.submit(scan_desktops)

            for future in as_completed(futures):
                key = futures[future]
                try:
                    discovered = future.result()
                    result.applications.extend(discovered.applications)
                    result.notices.extend(discovered.notices)
                    LOG.info(
                        "discovery_finished backend=%s count=%d",
                        key,
                        len(discovered.applications),
                    )
                except Exception as error:
                    LOG.warning("discovery_failed backend=%s error=%s", key, error)
                    result.notices.append(f"{key}: {error}")

            try:
                entries, notices = desktop_future.result()
                result.notices.extend(notices)
            except Exception as error:
                entries = []
                result.notices.append(f"Desktop entries: {error}")

        owners = dpkg_owners(entries, self.runner)
        result.applications = merge_applications(result.applications, entries, owners)
        LOG.info("deduplication_finished count=%d", len(result.applications))
        return result
