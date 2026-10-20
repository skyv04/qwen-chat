"""Native skill browsing and keyboard completion for the composer."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from .skills import SkillRegistry  # noqa: E402


class SkillPicker(Gtk.Popover):
    def __init__(self, registry: SkillRegistry, choose, create, save_workflow,
                 open_library, refocus, can_create, has_workflow):
        super().__init__(position=Gtk.PositionType.TOP, has_arrow=False, halign=Gtk.Align.START)
        self.add_css_class("skill-picker")
        self.registry = registry
        self.choose = choose
        self.refocus = refocus
        self.can_create = can_create
        self.has_workflow = has_workflow
        self.inline = False
        self.catalog = []
        self._preparing = False
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.set_child(self.body)
        heading = Gtk.Label(label="Suggest a skill", xalign=0)
        heading.add_css_class("heading")
        self.body.append(heading)
        hint = Gtk.Label(label="Qwen can also choose other skills and tools.", xalign=0, wrap=True)
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        self.body.append(hint)
        self.search = Gtk.SearchEntry(placeholder_text="Search skills")
        self.search.connect("search-changed", self._search_changed)
        self.body.append(self.search)
        self.warning = Gtk.Label(xalign=0, wrap=True, visible=False, lines=3,
                                 ellipsize=Pango.EllipsizeMode.END)
        self.warning.add_css_class("warning")
        self.warning.add_css_class("caption")
        self.body.append(self.warning)
        self.scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                           propagate_natural_height=True, max_content_height=280)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE,
                               activate_on_single_click=True)
        self.list.add_css_class("skill-list")
        self.list.connect("row-activated", lambda _list, row: self._choose(row))
        self.scroller.set_child(self.list)
        self.body.append(self.scroller)
        self.empty = Gtk.Label(label="No matching skills.", xalign=0, visible=False)
        self.empty.add_css_class("dim-label")
        self.body.append(self.empty)
        self.keys_hint = Gtk.Label(label="Up/Down to choose. Tab or Enter to insert.", xalign=0,
                                  wrap=True)
        self.keys_hint.add_css_class("dim-label")
        self.keys_hint.add_css_class("caption")
        self.body.append(self.keys_hint)
        self.body.append(Gtk.Separator())
        actions = Gtk.Box(spacing=6)
        self.create_button = Gtk.Button(label="Create a skill...")
        self.create_button.connect("clicked", lambda *_: self._action(create))
        actions.append(self.create_button)
        library = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Open shared skill library")
        library.add_css_class("flat")
        library.connect("clicked", lambda *_: self._action(open_library))
        actions.append(Gtk.Box(hexpand=True))
        actions.append(library)
        self.body.append(actions)
        self.save_button = Gtk.Button(label="Save this workflow as a skill")
        self.save_button.connect("clicked", lambda *_: self._action(save_workflow))
        self.body.append(self.save_button)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key_pressed)
        self.add_controller(keys)
        self.connect("closed", self._closed)

    def prepare(self, inline=False, query=""):
        self.inline = inline
        self.set_autohide(not inline)
        self.search.set_visible(not inline)
        self.keys_hint.set_visible(inline)
        self.catalog, errors = self.registry.catalog()
        self.warning.set_visible(bool(errors))
        self.warning.set_label("\n".join(errors[:2]) + (f"\n{len(errors) - 2} more errors." if len(errors) > 2 else ""))
        self.warning.set_tooltip_text("\n".join(errors) or None)
        self.create_button.set_sensitive(self.can_create())
        self.create_button.set_tooltip_text("Draft a reusable skill for your review" if self.can_create()
                                           else "Turn on Files to create and save skills")
        self.save_button.set_sensitive(self.can_create() and self.has_workflow())
        self.save_button.set_tooltip_text(
            "Turn on Files to create and save skills" if not self.can_create() else
            "Package the successful approach from this conversation" if self.has_workflow() else
            "Available after you start a task")
        root = self.get_root()
        width = root.get_width() if root is not None else 480
        height = root.get_height() if root is not None else 800
        self.body.set_size_request(min(400, max(230, width - 80)), -1)
        self.scroller.set_max_content_height(min(280, max(100, height - 360)))
        self._preparing = True
        self.search.set_text(query)
        self._preparing = False
        self.filter(query)
        GLib.idle_add(self._focus)

    def _focus(self):
        if self.get_visible():
            if self.inline:
                self.refocus()
            else:
                self.search.grab_focus()
        return False

    def show_inline(self, query):
        if not self.get_visible():
            self.prepare(inline=True, query=query)
            self.popup()
        elif self.inline:
            self.filter(query)

    def filter(self, query):
        query = query.strip().casefold()
        while (row := self.list.get_row_at_index(0)) is not None:
            self.list.remove(row)
        matches = [s for s in self.catalog if query in (s.name + " " + s.description).casefold()]
        matches.sort(key=lambda s: (not s.name.startswith(query), s.name))
        for skill in matches:
            row = Gtk.ListBoxRow()
            row.skill = skill
            row.set_tooltip_text(skill.description)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            title = Gtk.Box(spacing=10)
            name = Gtk.Label(label="/" + skill.name, xalign=0, hexpand=True,
                             ellipsize=Pango.EllipsizeMode.END)
            name.add_css_class("heading")
            title.append(name)
            origin = Gtk.Label(label="Built-in" if skill.builtin else "Saved")
            origin.add_css_class("dim-label")
            origin.add_css_class("caption")
            title.append(origin)
            box.append(title)
            description = Gtk.Label(label=skill.description, xalign=0, wrap=True, lines=2,
                                    wrap_mode=Pango.WrapMode.WORD_CHAR,
                                    ellipsize=Pango.EllipsizeMode.END, max_width_chars=48)
            description.add_css_class("caption")
            description.add_css_class("dim-label")
            box.append(description)
            row.set_child(box)
            self.list.append(row)
        self.empty.set_visible(not matches)
        self.scroller.set_visible(bool(matches))
        if matches:
            self.list.select_row(self.list.get_row_at_index(0))
        self.scroller.get_vadjustment().set_value(0)

    def _search_changed(self, entry):
        if not self._preparing and not self.inline:
            self.filter(entry.get_text())

    def move(self, delta):
        row = self.list.get_selected_row()
        index = (row.get_index() if row is not None else 0) + delta
        row = self.list.get_row_at_index(max(0, index))
        if row is not None:
            self.list.select_row(row)
            allocation = row.get_allocation()
            adj = self.scroller.get_vadjustment()
            if allocation.y < adj.get_value():
                adj.set_value(allocation.y)
            elif allocation.y + allocation.height > adj.get_value() + adj.get_page_size():
                adj.set_value(allocation.y + allocation.height - adj.get_page_size())

    def select_current(self):
        row = self.list.get_selected_row()
        if row is None:
            return False
        self._choose(row)
        return True

    def _choose(self, row):
        self.popdown()
        self.choose(row.skill.name)

    def _action(self, callback):
        self.popdown()
        callback()

    def _closed(self, *_):
        self.inline = False

    def _key_pressed(self, _ctrl, keyval, _code, state):
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        if keyval in (Gdk.KEY_Up, Gdk.KEY_Down):
            self.move(-1 if keyval == Gdk.KEY_Up else 1)
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            return self.select_current()
        if keyval == Gdk.KEY_Escape:
            self.popdown()
            self.refocus()
            return True
        return False
