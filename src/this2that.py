#!/usr/bin/env python3.10 

import os
import sys
import json
import logging
import pathlib
from datetime import datetime
from copy import deepcopy

from textual.app import App, ComposeResult
from textual.widgets import TextArea, Tree, Static
from textual.containers import Horizontal, Vertical
from textual import events
from textual.reactive import reactive
from textual.screen import ModalScreen
from rich.text import Text
from rich.panel import Panel
from rich.align import Align

import jmespath
from jinja2 import Environment, TemplateSyntaxError
from ruamel.yaml import YAML
from io import StringIO

# ------------------------------------------------------------------------------
# Logging Setup
# ------------------------------------------------------------------------------
DEBUG_LOG_FILE = os.environ.get("DEBUG_LOG")

logger = logging.getLogger("this2that")
logger.setLevel(logging.DEBUG)

if DEBUG_LOG_FILE:
    fh = logging.FileHandler(DEBUG_LOG_FILE, mode='w', encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        fmt='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    logger.info("Debug logging enabled. Writing logs to %s", DEBUG_LOG_FILE)
else:
    logger.addHandler(logging.NullHandler())

# ------------------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------------------
SAVE_FILE = os.path.expanduser("~/.config/this2that/saved_runs.yaml")

DEFAULT_CONFIG = {
    "keys": {
        "help": ["ctrl+/", "ctrl+underscore", "?"],  # VMware safe mapping
        "quit": ["ctrl+x", "ctrl+q"],
        "save": ["ctrl+s"],
        "refresh": ["enter", "ctrl+enter"],
        "edit_toggle": ["ctrl+e"],
        "ai_suggest": ["ctrl+a"],  # AI Suggestion trigger
        "search_toggle": ["ctrl+f"],
        "output_json": ["ctrl+j"],   
        "output_yaml": ["ctrl+y"],  
        "output_yaml_nice": ["ctrl+shift+y","ctrl+n"] 
    }
}

yaml_parser = YAML(typ="safe")

# ------------------------------------------------------------------------------
# Utility Functions
# ------------------------------------------------------------------------------
def yaml_or_json_load(text: str):
    try:
        return yaml_parser.load(text)
    except Exception:
        return json.loads(text)

def ensure_save_file():
    path = pathlib.Path(SAVE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with open(path, "w") as f:
            f.write("[]\n")

def normalize_expression(expr: str) -> str:
    stripped = expr.strip()

    if not stripped:
        return stripped

    # If the user already wrapped in {{ }}, leave it as-is
    if stripped.startswith("{{") and stripped.endswith("}}"):
        return stripped

    # If it contains parentheses or 'selected |', assume it's already a filter chain
    if "(" in stripped or stripped.startswith("selected |"):
        return "{{ " + stripped + " }}"

    # Otherwise, treat as a simple filter name
    return "{{ selected | " + stripped + " }}"


# ------------------------------------------------------------------------------
# Jinja2 Environment with json_query Support
# ------------------------------------------------------------------------------
def setup_jinja_environment():
    env = Environment()
    try:
        from ansible.plugins.filter.core import FilterModule as AnsibleFilters
        env.filters.update(AnsibleFilters().filters())
        logger.info("Loaded Ansible filters: %s", list(env.filters.keys()))

    except Exception as e:
        logger.warning("Failed to load Ansible filters: %s", e)

    # Ensure json_query exists
    if "json_query" not in env.filters:
        logger.warning("json_query not found, using fallback.")
        def json_query(data, expression):
            try:
                return jmespath.search(expression, data)
            except Exception as e:
                logger.error("json_query error: %s", e)
                return None
        env.filters["json_query"] = json_query

    return env

# ------------------------------------------------------------------------------
# Config Loader
# ------------------------------------------------------------------------------
def load_user_config():
    config_path = os.environ.get(
        "THIS2THAT_CONFIG",
        os.path.expanduser("~/.config/this2that/config.yaml")
    )
    path = pathlib.Path(config_path)
    if path.exists():
        try:
            with open(path, "r") as f:
                user_config = yaml_parser.load(f)
            merged = deepcopy(DEFAULT_CONFIG)
            merged["keys"].update(user_config.get("keys", {}))
            return merged
        except Exception as e:
            logger.warning("Failed to load config: %s", e)
            return DEFAULT_CONFIG
    return DEFAULT_CONFIG

# ------------------------------------------------------------------------------
# Help Modal
# ------------------------------------------------------------------------------
class HelpModal(ModalScreen):
    def compose(self) -> ComposeResult:
        help_text = """
[b]This2That Ansible Data transformation tool - Help[/b]

Author: Steve Maher

Navigate JSON or YAML data, apply Jinja2 filters, and save transformations.

[b]Key Bindings:[/b]
  • Ctrl+/ or Ctrl+_ or ?  - Show/Hide this help
  • Ctrl+Q                 - Quit
  • Ctrl+S                 - Save current run
  • Enter                  - Expand/Collapse node
  • Ctrl+Enter             - Refresh output
  • Ctrl+E                 - Toggle output editor edit mode
  • Ctrl+A                 - AI suggest filter from edited output
  • Ctrl+J                 - Output format: JSON
  • Ctrl+Y                 - Output format: YAML
  • Ctrl+Shift+Y           - Output format: YAML (pretty / expanded)
  • Ctrl+F                 - (Reserved for search mode)

[b]Usage Notes:[/b]
- Select a node in the tree to view its data.
- Type a filter below to transform selected data.
- json_query and Jinja2 filters are supported.
- Edit right pane (Ctrl+E), change output, then press Ctrl+A to get a suggested filter.
        """
        yield Static(
            Panel(Align.center(help_text, vertical="middle"),
                title="Help - This2That", border_style="cyan"),
            id="help_modal_content"
        )

    def on_key(self, event: events.Key):
        if event.key in ["escape", "/", "?", "ctrl+underscore"]:
            self.app.pop_screen()

# ------------------------------------------------------------------------------
# Main Application
# ------------------------------------------------------------------------------
class This2That(App):
    CSS = """
    Horizontal { height: 1fr; }
    Tree { width: 40%; border: solid green; }
    TextArea#output_editor { width: 60%; border: solid blue; overflow: auto; }
    TextArea#expr_input { height: 6; border: solid yellow; }
    TextArea#expr_input.error { background: #330000; color: #ffcccc; }
    Static#suggestion_bar { height: 3; border: solid cyan; }
    """

    show_search = reactive(False)
    edit_right = reactive(False)

    def __init__(self, data_file: str):
        super().__init__()
        self.data_file = data_file
        self.data = None
        self.data_load_error = None
        self.selected_value = None
        self.node_map = {}

        self.config = load_user_config()
        self.j2_env = setup_jinja_environment()
        self.suppress_highlight_refresh = False

        self.output_format = "json"  # default output format
        self.yaml_pretty = False      # pretty YAML mode toggle


    # ----------------------------------------------------------------------
    # Key Utilities
    # ----------------------------------------------------------------------
    def is_key(self, action, key):
        return key.lower() in [k.lower() for k in self.config["keys"].get(action, [])]

    # ----------------------------------------------------------------------
    # Data Loading
    # ----------------------------------------------------------------------
    def load_data(self):
        try:
            with open(self.data_file, "r") as f:
                content = f.read()
            try:
                content = yaml_parser.load(content)
                logger.info("Loaded data as YAML. from %s", self.data_file)
                return content
            
            except Exception:
                logger.info("Highlight refresh suppressed temporarily.")
                return json.loads(content)
        except Exception as e:
            logger.info("load_data Failed to load data file: %s, error:", self.data_file, str(e))      

            self.data_load_error = str(e)
            return None 

    # ----------------------------------------------------------------------
    # Layout
    # ----------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Vertical():
            with Horizontal():
                yield Tree("Data", id="data_tree")
                self.output_editor = TextArea(id="output_editor")
                self.output_editor.read_only = True
                yield self.output_editor

            self.suggestion_bar = Static(id="suggestion_bar")
            yield self.suggestion_bar

            self.expr_input = TextArea(
                placeholder='Enter a filter, e.g., json_query("[].name") or length',
                id="expr_input",
            )
            yield self.expr_input

    # ----------------------------------------------------------------------
    # Tree Setup
    # ----------------------------------------------------------------------
    def on_mount(self):
        ensure_save_file()
        self.data_tree = self.query_one("#data_tree", Tree)
        self.data = self.load_data()

        if self.data_load_error:
            self.data_tree.root.set_label("Invalid Input File")
            self.data_tree.root.add_leaf(Text(f"Error: {self.data_load_error}", style="bold red"))
            self.data_tree.root.expand()
            return

        self.build_tree(self.data, self.data_tree.root, path=[])
        self.data_tree.root.expand()

    def build_tree(self, data, node, path):
        if isinstance(data, dict):
            for key, value in data.items():
                cur = path + [key]
                child = node.add(f"{key}:")
                self.node_map[child.id] = (key, child, cur)
                self.build_tree(value, child, cur)
        elif isinstance(data, list):
            for i, value in enumerate(data):
                cur = path + [i]
                child = node.add(f"[{i}]")
                self.node_map[child.id] = (str(i), child, cur)
                self.build_tree(value, child, cur)
        else:
            leaf = node.add(str(data))
            self.node_map[leaf.id] = (str(data), leaf, path)

    # ----------------------------------------------------------------------
    # Tree Navigation
    # ----------------------------------------------------------------------
    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted):
        if self.suppress_highlight_refresh:
            logger.debug("Highlight refresh suppressed temporarily.")
            return

        _, _, path = self.node_map.get(event.node.id, (None, None, None))
        if path is not None:
            self.selected_value = self.get_value_at_path(path, self.data)
            logger.debug("Node highlighted. Path: %s", path)
            self.refresh_output(force=True)

    def on_tree_node_selected(self, event: Tree.NodeSelected):
        self.suppress_highlight_refresh = True
        self.set_timer(0.12, lambda: setattr(self, "suppress_highlight_refresh", False))

    def get_value_at_path(self, path, data):
        cur = data
        for k in path:
            if isinstance(cur, dict):
                cur = cur.get(k)
            elif isinstance(cur, list) and isinstance(k, int):
                cur = cur[k]
            else:
                return None
        return cur

    # ----------------------------------------------------------------------
    # Expression Evaluation
    # ----------------------------------------------------------------------
    def refresh_output(self, force=False):
        expr_raw = self.expr_input.text.strip()
        if not expr_raw and self.selected_value is not None:
            self.pretty_update_right(self.selected_value)
            self.clear_error_highlight()
            return

        if not self.selected_value:
            self.output_editor.text = "Select a node in the tree to evaluate."
            return

        expr = normalize_expression(expr_raw)

        try:
            result = self.evaluate_expression(expr, self.selected_value)
            try:
                obj = yaml_or_json_load(result) if isinstance(result, str) else result
            except Exception:
                obj = result
            self.pretty_update_right(obj)
            self.clear_error_highlight()
        except TemplateSyntaxError as e:
            self.show_inline_error(f"Syntax Error: {e.message}")
        except RuntimeError as e:
            self.show_inline_error(f"Runtime Error: {e}")

    def evaluate_expression(self, expr, value):
        try:
            template = self.j2_env.from_string(expr)
            return template.render(selected=value)
            
        except TemplateSyntaxError as e:
            raise TemplateSyntaxError(str(e), e.lineno, e.name, e.filename)
        
        except Exception as e:
            raise RuntimeError(str(e))

    def pretty_update_right(self, result):
        """Render result as JSON, YAML, or Nice YAML based on current settings."""
        try:
            if self.output_format == "yaml":
                try:
                    stream = StringIO()
                    # Use nice expanded formatting if yaml_pretty is True
                    yaml_parser.default_flow_style = False if self.yaml_pretty else None
                    yaml_parser.dump(result, stream)
                    yaml_str = stream.getvalue()
                    self.output_editor.text = yaml_str
                except Exception as e:
                    self.output_editor.text = f"ERROR: Failed to render as YAML\n{str(e)}"
                return

            # JSON output
            if isinstance(result, (dict, list)):
                self.output_editor.text = json.dumps(result, indent=2, ensure_ascii=False)
                return

            if isinstance(result, str):
                s = result.strip()
                if s.startswith("{") or s.startswith("["):
                    try:
                        self.output_editor.text = json.dumps(json.loads(result), indent=2, ensure_ascii=False)
                        return
                    except json.JSONDecodeError:
                        pass
                self.output_editor.text = result
                return

            self.output_editor.text = str(result)

        except Exception as e:
            self.output_editor.text = f"ERROR: Failed to render output\n{str(e)}"

    # ----------------------------------------------------------------------
    # Error Handling
    # ----------------------------------------------------------------------
    def show_inline_error(self, message: str):
        self.expr_input.add_class("error")
        self.output_editor.text = f"ERROR: {message}"

    def clear_error_highlight(self):
        if "error" in self.expr_input.classes:
            self.expr_input.remove_class("error")

    # ----------------------------------------------------------------------
    # Edit Mode and AI Suggestion
    # ----------------------------------------------------------------------
    def toggle_edit_mode(self):
        """Toggle right pane between read-only and editable, only changing border color."""
        self.edit_right = not self.edit_right
        self.output_editor.read_only = not self.edit_right

        if self.edit_right:
            self.output_editor.border_title = "Output (EDIT MODE)"
            self.output_editor.styles.border_color = "yellow"  # Only change color
        else:
            self.output_editor.border_title = "Output"
            self.output_editor.styles.border_color = "blue"    # Only change color

        logger.debug("Right pane edit mode toggled: %s", self.edit_right)

    def ai_suggest_filter(self):
        if not self.selected_value:
            self.output_editor.text = "No node selected, cannot suggest filter."
            return

        try:
            desired_output = yaml_or_json_load(self.output_editor.text)
        except Exception as e:
            self.output_editor.text = f"ERROR: Desired output is invalid YAML/JSON: {e}"
            return

        suggested_filter = self.heuristic_suggest(self.selected_value, desired_output)

        if suggested_filter:
            self.expr_input.text = suggested_filter
            self.output_editor.text = (
                f"Suggested filter:\n\n{suggested_filter}\n\n"
                f"Press Enter to test or modify it."
            )
        else:
            self.output_editor.text = "AI could not determine a suitable filter."

    def heuristic_suggest(self, input_data, output_data):
        if input_data == output_data:
            return "selected | to_yaml"
        if isinstance(input_data, list) and isinstance(output_data, list):
            if len(output_data) < len(input_data):
                return "selected | json_query('[*]')"
        if isinstance(output_data, int) and isinstance(input_data, (list, dict)):
            return "selected | length"
        return None

    # ----------------------------------------------------------------------
    # Save Feature
    # ----------------------------------------------------------------------
    def save_current_run(self):
        if not self.selected_value:
            self.output_editor.text = "No node selected, cannot save."
            return

        expr_raw = self.expr_input.text.strip()
        if not expr_raw:
            self.output_editor.text = "No expression entered, cannot save."
            return

        filter_expr = normalize_expression(expr_raw)
        try:
            output_data = yaml_or_json_load(self.output_editor.text)
        except Exception:
            output_data = self.output_editor.text

        try:
            with open(SAVE_FILE, "r") as f:
                existing = yaml_parser.load(f) or []
        except Exception:
            existing = []

        record = {
            "timestamp": datetime.now().isoformat(),
            "filter": filter_expr,
            "input": self.selected_value,
            "output": output_data,
        }
        existing.append(record)

        with open(SAVE_FILE, "w") as f:
            yaml_parser.dump(existing, f)

        self.output_editor.text = f"Saved current run to {SAVE_FILE}"

    # ----------------------------------------------------------------------
    # Key Handling
    # ----------------------------------------------------------------------
    def on_text_area_changed(self, event: TextArea.Changed):
        if event.control is self.expr_input:
            self.refresh_output(force=True)

    def on_key(self, event: events.Key):
        ctrl = getattr(event, "ctrl", False)
        alt = getattr(event, "alt", False)
        shift = getattr(event, "shift", False)

        logger.debug(
            "Key pressed: key=%s ctrl=%s alt=%s shift=%s",
            event.key, ctrl, alt, shift
        )

        if self.is_key("quit", event.key):
            self.exit()

        elif self.is_key("help", event.key):
            self.push_screen(HelpModal())

        elif self.is_key("refresh", event.key):
            self.refresh_output(force=True)

        elif self.is_key("save", event.key):
            self.save_current_run()

        elif self.is_key("edit_toggle", event.key):
            self.toggle_edit_mode()

        elif self.is_key("ai_suggest", event.key):
            self.ai_suggest_filter()

        elif self.is_key("output_json", event.key):
            self.output_format = "json"
            self.suggestion_bar.update("[green]Output format changed to JSON[/green]")
            self.refresh_output(force=True)

        elif self.is_key("output_yaml", event.key):
            self.output_format = "yaml"
            self.suggestion_bar.update("[green]Output format changed to YAML[/green]")
            self.refresh_output(force=True)

        elif self.is_key("output_yaml_nice", event.key):
            self.output_format = "yaml"
            self.yaml_pretty = True
            self.suggestion_bar.update("[green]Output format changed to YAML (pretty)[/green]")
            self.refresh_output(force=True)

# ------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python this2that.py <file.yaml|file.json>")
        sys.exit(1)

    app = This2That(sys.argv[1])
    app.run()
