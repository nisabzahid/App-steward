import datetime
import json
import logging
import os
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, GObject, Gtk, Pango

from .discovery import DiscoveryService
from .model import Application, fuzzy_match, human_size
from .operations import OperationService

LOG = logging.getLogger(__name__)

FILTERS = ["All", "APT", "Flatpak", "Snap", "AppImage", "Other"]
SORTS = ["Name A–Z", "Name Z–A", "Size", "Installation type", "Version"]
VIEWS = ["Desktop applications", "Command-line / other software"]


class RowObject(GObject.Object):
    def __init__(self, application: Application):
        super().__init__()
        self.application = application


def button(label, callback):
    widget = Gtk.Button(label=label)
    widget.connect("clicked", callback)
    return widget


def wrapped_label(text: str, selectable: bool = False) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0)
    label.set_wrap(True)
    label.set_selectable(selectable)
    return label


def set_icon(image: Gtk.Image, icon: str) -> None:
    try:
        if icon and os.path.isabs(icon) and Path(icon).is_file():
            image.set_from_gicon(Gio.FileIcon.new(Gio.File.new_for_path(icon)))
        else:
            image.set_from_icon_name(icon or "application-x-executable")
    except Exception:
        image.set_from_icon_name("application-x-executable")
    image.set_pixel_size(40)


class ManagerWindow(Gtk.ApplicationWindow):
    def __init__(self, application):
        super().__init__(
            application=application,
            title="App Steward",
            default_width=1000,
            default_height=720,
        )
        self.discovery = DiscoveryService()
        self.operations = OperationService()
        self.applications: list[Application] = []
        self.notices: list[str] = []
        self.busy = False
        self.search_timeout = 0
        self.last_refresh = None

        self.connect("close-request", self.on_close)

        header = Gtk.HeaderBar()
        self.set_titlebar(header)
        self.refresh_button = button("Refresh", lambda _: self.refresh())
        header.pack_end(self.refresh_button)
        header.pack_start(button("Diagnostics", lambda _: self.show_diagnostics()))

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(root, f"set_margin_{side}")(18)
        self.set_child(root)

        title = Gtk.Label(label="App Steward", xalign=0)
        title.add_css_class("title-1")
        root.append(title)

        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text(
            "Search name, package, description, publisher, or source"
        )
        self.search.connect("search-changed", self.on_search)
        root.append(self.search)

        controls = Gtk.Box(spacing=10)
        self.filter = Gtk.DropDown.new_from_strings(FILTERS)
        self.sort = Gtk.DropDown.new_from_strings(SORTS)
        self.view = Gtk.DropDown.new_from_strings(VIEWS)
        self.system_toggle = Gtk.CheckButton(label="Show system packages")

        for label, widget in (
            ("Filter", self.filter),
            ("Sort", self.sort),
            ("View", self.view),
        ):
            controls.append(Gtk.Label(label=label))
            controls.append(widget)
            widget.connect("notify::selected", lambda *_: self.rebuild())
        self.system_toggle.connect("toggled", lambda *_: self.rebuild())

        root.append(controls)
        root.append(self.system_toggle)

        self.store = Gio.ListStore.new(RowObject)
        self.selection = Gtk.SingleSelection.new(self.store)
        self.selection.set_autoselect(False)
        self.selection.set_can_unselect(True)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self.row_setup)
        factory.connect("bind", self.row_bind)

        self.list_view = Gtk.ListView.new(self.selection, factory)
        self.list_view.set_single_click_activate(True)
        self.list_view.connect("activate", self.row_activated)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_child(self.list_view)
        root.append(scroll)

        self.progress = Gtk.ProgressBar()
        self.progress.set_visible(False)
        root.append(self.progress)

        self.status = Gtk.Label(label="Ready", xalign=0)
        self.status.set_wrap(True)
        root.append(self.status)

        GLib.timeout_add(120, self.pulse)
        self.refresh()

    def on_close(self, *_):
        if self.busy:
            self.message(
                "Operation in progress",
                "Wait for discovery, planning, or package operations to finish. "
                "Interrupting a package manager can leave an incomplete transaction.",
            )
            return True
        return False

    def pulse(self):
        if self.busy:
            self.progress.pulse()
        return True

    def set_busy(self, busy: bool, status: str = ""):
        self.busy = busy
        self.refresh_button.set_sensitive(not busy)
        self.progress.set_visible(busy)
        if status:
            self.status.set_text(status)

    def background(self, work, done, failure=None):
        def worker():
            try:
                result = work()
                GLib.idle_add(deliver, result)
            except Exception as error:
                LOG.warning("background_operation_failed error=%s", error)
                GLib.idle_add(failed, str(error))

        def deliver(result):
            done(result)
            return False

        def failed(text):
            self.set_busy(False, "Operation failed.")
            if failure:
                failure(text)
            else:
                self.message("Operation failed", text)
            return False

        threading.Thread(target=worker, daemon=False).start()

    def refresh(self):
        if self.busy:
            return
        self.set_busy(True, "Discovering software…")

        def complete(result):
            self.applications = result.applications
            self.notices = result.notices
            self.last_refresh = datetime.datetime.now().astimezone()
            self.set_busy(False)
            self.rebuild()

        self.background(self.discovery.discover, complete)

    def on_search(self, *_):
        if self.search_timeout:
            GLib.source_remove(self.search_timeout)
        self.search_timeout = GLib.timeout_add(100, self.apply_search)

    def apply_search(self):
        self.search_timeout = 0
        self.rebuild()
        return False

    def rebuild(self):
        query = self.search.get_text()
        source = FILTERS[self.filter.get_selected()]
        advanced = self.system_toggle.get_active()
        cli = self.view.get_selected() == 1
        selected = []

        for application in self.applications:
            if not advanced:
                if cli:
                    if application.desktop_application:
                        continue
                    if (
                        application.installation_type == "APT"
                        and application.metadata.get("automatic") is not False
                    ):
                        continue
                    if (
                        application.installation_type == "APT"
                        and application.metadata.get("priority")
                        in {"required", "important"}
                    ):
                        continue
                    if (
                        application.installation_type == "DesktopEntry"
                        and application.system_package
                    ):
                        continue
                elif not application.desktop_application:
                    continue

            if source == "Other":
                if application.installation_type in {
                    "APT",
                    "Flatpak",
                    "Snap",
                    "AppImage",
                }:
                    continue
            elif source != "All" and source != application.installation_type:
                continue

            if fuzzy_match(query, application):
                selected.append(application)

        sort = self.sort.get_selected()
        if sort == 2:
            selected.sort(
                key=lambda item: (
                    -(item.size if item.size is not None else -1),
                    item.name.casefold(),
                )
            )
        elif sort == 3:
            selected.sort(
                key=lambda item: (item.installation_type, item.name.casefold())
            )
        elif sort == 4:
            selected.sort(
                key=lambda item: (item.version.casefold(), item.name.casefold())
            )
        else:
            selected.sort(
                key=lambda item: item.name.casefold(),
                reverse=sort == 1,
            )

        self.store.splice(
            0,
            self.store.get_n_items(),
            [RowObject(application) for application in selected],
        )
        if not self.busy:
            timestamp = (
                self.last_refresh.strftime("%H:%M:%S %Z")
                if self.last_refresh
                else "not yet"
            )
            self.status.set_text(
                f"{len(selected)} shown / {len(self.applications)} discovered · "
                f"Last refreshed: {timestamp} · "
                f"{len(self.notices)} diagnostic notice(s)"
            )

    def row_setup(self, factory, item):
        row = Gtk.Box(spacing=14)
        row.set_margin_top(10)
        row.set_margin_bottom(10)
        row.set_margin_start(8)
        row.set_margin_end(8)
        image = Gtk.Image()
        row.append(image)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        text.set_hexpand(True)
        name = Gtk.Label(xalign=0)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.add_css_class("heading")
        subtitle = Gtk.Label(xalign=0)
        subtitle.set_ellipsize(Pango.EllipsizeMode.END)
        subtitle.add_css_class("dim-label")
        text.append(name)
        text.append(subtitle)
        row.append(text)

        version = Gtk.Label(xalign=1)
        version.set_max_width_chars(24)
        version.set_ellipsize(Pango.EllipsizeMode.END)
        row.append(version)

        size = Gtk.Label(xalign=1, width_chars=12)
        size.add_css_class("dim-label")
        row.append(size)

        update = button("Update", lambda _: self.update_row(item))
        update.set_visible(False)
        row.append(update)

        item.set_child(row)
        item.widgets = image, name, subtitle, version, size, update

    def row_bind(self, factory, item):
        application = item.get_item().application
        image, name, subtitle, version, size, update = item.widgets
        set_icon(image, application.icon)
        name.set_text(application.name)
        subtitle.set_text(
            f"{application.installation_type} · "
            f"{application.publisher or application.source}"
        )
        version.set_text(application.version or "Version unknown")
        size.set_text(human_size(application.size))
        update.set_visible(application.can_update)
        item.application = application

    def update_row(self, item):
        if self.busy:
            return
        self.prepare_update(item.application, None)

    def row_activated(self, widget, position):
        if self.busy:
            return
        row = self.store.get_item(position)
        if row:
            self.show_details(row.application)

    def text_window(self, title: str, text: str):
        window = Gtk.Window(
            title=title,
            transient_for=self,
            modal=True,
            default_width=740,
            default_height=500,
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(16)
        window.set_child(box)

        view = Gtk.TextView(editable=False, cursor_visible=False)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        view.get_buffer().set_text(text)
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_child(view)
        box.append(scroll)
        box.append(button("Close", lambda _: window.close()))
        window.present()
        return window, box, view

    def message(self, title: str, text: str):
        self.text_window(title, text)

    def show_diagnostics(self):
        self.message(
            "Discovery diagnostics",
            (
                "\n\n".join(self.notices)
                if self.notices
                else (
                    "All available discovery backends completed without "
                    "reported errors."
                )
            ),
        )

    def show_details(self, application: Application):
        details = (
            f"{application.name}\n\n"
            f"{application.description or 'No description available.'}\n\n"
            f"Version: {application.version or 'Unknown'}\n"
            f"Type: {application.installation_type}\n"
            f"Package / ID: {application.package_name or application.id}\n"
            f"Source: {application.source or 'Unknown'}\n"
            f"Publisher: {application.publisher or 'Unknown'}\n"
            f"Architecture: {application.architecture or 'Unknown'}\n"
            f"Approximate installed size: {human_size(application.size)}\n"
            f"Location: {application.install_location or 'Package-managed paths'}\n"
            f"Categories: {application.category or 'Unknown'}\n"
            f"Desktop files:\n"
            + ("\n".join(application.desktop_files) or "None")
            + "\n\nMetadata:\n"
            + json.dumps(application.metadata, ensure_ascii=False, indent=2)
        )
        window, box, _ = self.text_window(application.name, details)
        actions = Gtk.Box(spacing=10)
        launch = button(
            "Launch",
            lambda _: self.launch(application),
        )
        launch.set_sensitive(bool(application.desktop_files))
        actions.append(launch)

        update = button(
            "Update",
            lambda _: self.prepare_update(application, window),
        )
        update.set_sensitive(application.can_update)
        actions.append(update)

        reinstall = button(
            "Reinstall",
            lambda _: self.choose_reinstall_source(application, window),
        )
        reinstall.set_sensitive(bool(self.operations.reinstall_sources(application)))
        actions.append(reinstall)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        actions.append(spacer)

        uninstall = button(
            "Uninstall",
            lambda _: self.prepare_removal(application, window),
        )
        uninstall.add_css_class("destructive-action")
        uninstall.set_sensitive(application.can_uninstall)
        actions.append(uninstall)
        box.prepend(actions)

    def choose_reinstall_source(self, application, details_window):
        sources = self.operations.reinstall_sources(application)
        if not sources or self.busy:
            return
        window = Gtk.Window(
            title=f"Reinstall {application.name}",
            transient_for=self,
            modal=True,
            default_width=560,
            default_height=240,
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        window.set_child(box)
        box.append(wrapped_label("Choose the package source for this reinstall."))
        source = Gtk.DropDown.new_from_strings(sources)
        box.append(source)
        actions = Gtk.Box(spacing=10)
        cancel = button("Cancel", lambda _: window.close())
        actions.append(cancel)

        def continue_to_plan(_):
            selected = sources[source.get_selected()]
            window.close()
            details_window.close()
            self.set_busy(True, "Preparing and validating reinstall plan…")

            def ready(plan):
                self.set_busy(False, "Review the reinstall plan.")
                self.confirm_reinstall(application, plan)

            self.background(
                lambda: self.operations.reinstall_plan(application, selected),
                ready,
            )

        accept = button("Continue", continue_to_plan)
        actions.append(accept)
        box.append(actions)
        window.set_default_widget(cancel)
        window.present()

    def launch(self, application):
        if not application.desktop_files:
            return
        try:
            desktop = Gio.DesktopAppInfo.new_from_filename(application.desktop_files[0])
            if desktop is None:
                raise RuntimeError("Desktop launcher is no longer valid.")
            desktop.launch([], self.get_display().get_app_launch_context())
        except Exception as error:
            self.message("Launch failed", str(error))

    def prepare_removal(self, application, details_window):
        if self.busy:
            return
        self.confirm_removal_request(application, details_window)

    def confirm_removal_request(self, application, details_window):
        window = Gtk.Window(
            title=f"Uninstall {application.name}?",
            transient_for=self,
            modal=True,
            default_width=520,
            default_height=220,
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        window.set_child(box)
        box.append(
            wrapped_label(
                f"Are you sure you want to uninstall {application.name}? "
                "The application will be removed only after you review the "
                "operation plan."
            )
        )
        actions = Gtk.Box(spacing=10)
        cancel = button("Cancel", lambda _: window.close())
        actions.append(cancel)

        def continue_to_plan(_):
            window.close()
            details_window.close()
            self.set_busy(True, "Preparing and validating removal plan…")

            def ready(plan):
                self.set_busy(False, "Review the removal plan.")
                self.confirm_removal(application, plan)

            self.background(
                lambda: self.operations.plan(application),
                ready,
            )

        accept = button("Continue", continue_to_plan)
        accept.add_css_class("destructive-action")
        actions.append(accept)
        box.append(actions)
        window.set_default_widget(cancel)
        window.present()

    def prepare_update(self, application, details_window):
        if self.busy:
            return
        if details_window is not None:
            details_window.close()
        self.set_busy(True, "Preparing and validating update plan…")

        def ready(plan):
            self.set_busy(False, "Review the update plan.")
            self.confirm_update(application, plan)

        self.background(
            lambda: self.operations.update_plan(application),
            ready,
        )

    def confirm_update(self, application, plan):
        window = Gtk.Window(
            title=plan.title,
            transient_for=self,
            modal=True,
            default_width=680,
            default_height=450,
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        window.set_child(box)
        title = wrapped_label(plan.title)
        title.add_css_class("title-2")
        box.append(title)
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_child(wrapped_label(plan.explanation, selectable=True))
        box.append(scroll)
        actions = Gtk.Box(spacing=10)
        cancel = button("Cancel", lambda _: window.close())
        actions.append(cancel)

        def accept_update(_):
            window.close()
            self.execute_update(application, plan)

        accept = button("Update", accept_update)
        actions.append(accept)
        box.append(actions)
        window.set_default_widget(cancel)
        window.present()

    def confirm_reinstall(self, application, plan):
        window = Gtk.Window(
            title=plan.title,
            transient_for=self,
            modal=True,
            default_width=680,
            default_height=450,
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        window.set_child(box)
        title = wrapped_label(plan.title)
        title.add_css_class("title-2")
        box.append(title)
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_child(wrapped_label(plan.explanation, selectable=True))
        box.append(scroll)
        actions = Gtk.Box(spacing=10)
        cancel = button("Cancel", lambda _: window.close())
        actions.append(cancel)

        def accept_reinstall(_):
            window.close()
            self.execute_operation(application, plan, "Reinstalling", "Reinstall")

        actions.append(button("Reinstall", accept_reinstall))
        box.append(actions)
        window.set_default_widget(cancel)
        window.present()

    def execute_update(self, application, plan):
        self.execute_operation(application, plan, "Updating", "Update")

    def confirm_removal(self, application, plan):
        window = Gtk.Window(
            title=plan.title,
            transient_for=self,
            modal=True,
            default_width=680,
            default_height=450,
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        window.set_child(box)
        title = wrapped_label(plan.title)
        title.add_css_class("title-2")
        box.append(title)
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_child(wrapped_label(plan.explanation, selectable=True))
        box.append(scroll)
        actions = Gtk.Box(spacing=10)
        cancel = button("Cancel", lambda _: window.close())
        actions.append(cancel)

        def confirm(_):
            window.close()
            self.execute_removal(application, plan)

        accept = button("Uninstall", confirm)
        accept.add_css_class("destructive-action")
        actions.append(accept)
        box.append(actions)
        window.set_default_widget(cancel)
        window.present()

    def execute_removal(self, application, plan):
        self.execute_operation(application, plan, "Uninstalling", "Uninstallation")

    def execute_operation(self, application, plan, action, completed_label):
        self.set_busy(True, f"{action} {application.name}…")
        window, box, view = self.text_window(
            f"{action} {application.name}",
            "Starting package operation…\n",
        )
        window.connect("close-request", lambda *_: self.busy)
        buffer = view.get_buffer()
        lines = []

        def append(line):
            lines.append(line)
            if len(lines) > 1500:
                del lines[:300]
            buffer.set_text("\n".join(lines))
            return False

        def output(line):
            GLib.idle_add(append, line)

        def finished(code):
            self.set_busy(False)
            if code == 0:
                append(f"\n{application.name} was successfully completed.")
                window.set_title(f"{completed_label} completed")
            else:
                append(
                    f"\nOperation failed or was cancelled. Exit code: {code}\n"
                    "Review the output above. Authentication cancellation, "
                    "package locks, or package-manager errors may be responsible."
                )
                window.set_title(f"{completed_label} failed")
            self.refresh()

        def failed(text):
            append(f"\nOperation failed: {text}")
            window.set_title(f"{completed_label} failed")
            self.refresh()

        self.background(
            lambda: self.operations.execute(plan, output),
            finished,
            failed,
        )


class ManagerApplication(Gtk.Application):
    def __init__(self):
        super().__init__(
            application_id="io.github.installedsoftware.Manager",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )

    def do_activate(self):
        window = self.get_active_window()
        if window is None:
            window = ManagerWindow(self)
        window.present()
