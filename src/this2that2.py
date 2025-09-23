from textual.app import App, ComposeResult
from textual.widgets import TextArea, Tree, Static, Input
from textual.containers import Horizontal, Vertical
from textual import events
from textual.reactive import reactive
from rich.text import Text
from rich.panel import Panel
from rich.syntax import Syntax
import jmespath

import json
from copy import deepcopy
from collections import deque
from jinja2 import Environment, TemplateSyntaxError
from ansible.plugins.filter.core import FilterModule as AnsibleFilters
from ruamel.yaml import YAML

# ----------------------------
# YAML parser configured safely
# ----------------------------
yaml_parser = YAML(typ="safe")

def yaml_or_json_load(text: str):
    """Parse YAML or JSON; raise if both fail."""
    try:
        return yaml_parser.load(text)
    except Exception:
        return json.loads(text)

# --- Helpers --------------------------------------------------------------

def deep_equal(a, b):
    return a == b

def find_subnode_path(root, target):
    """BFS find exact-equal subnode; return path list (keys/indices) or None."""
    if deep_equal(root, target):
        return []
    q = deque([([], root)])
    seen = set()
    while q:
        path, node = q.popleft()
        try:
            if id(node) in seen:
                continue
            seen.add(id(node))
        except Exception:
            pass
        if isinstance(node, dict):
            for k, v in node.items():
                if deep_equal(v, target):
                    return path + [k]
                q.append((path + [k], v))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                if deep_equal(v, target):
                    return path + [i]
                q.append((path + [i], v))
    return None

def jinja_path_expr(path):
    """Jinja access expression from path (dotted/bracket)."""
    parts = ["selected"]
    for p in path:
        if isinstance(p, int):
            parts.append(f"[{p}]")
        else:
            if isinstance(p, str) and p.isidentifier():
                parts.append(f".{p}")
            else:
                s = p.replace("'", "\\'")
                parts.append(f"['{s}']")
    return "{{ " + "".join(parts) + " }}"

def path_to_jmespath(path):
    """Convert a Python path list to a JMESPath string."""
    parts = []
    for p in path:
        if isinstance(p, int):
            parts.append(f"[{p}]")
        else:
            if isinstance(p, str) and p.isidentifier():
                if parts:
                    parts.append("." + p)
                else:
                    parts.append(p)
            else:
                s = p.replace("'", "\\'")
                parts.append(f"['{s}']")
    return "".join(parts) if parts else "@"

def json_query_expr(jq: str):
    """Wrap a JMESPath string as a json_query Jinja expression."""
    return "{{ selected | json_query(" + json.dumps(jq) + ") }}"

def pretty_json(obj):
    return json.dumps(obj, indent=2, ensure_ascii=False)

# --- AI Suggestion Heuristics ---------------------------------------------

def detect_projection(source, target):
    """map(attribute=...) OR json_query('[].key')"""
    if not (isinstance(source, list) and isinstance(target, list) and source):
        return None
    if not all(isinstance(x, dict) for x in source):
        return None
    common = set(source[0].keys())
    for it in source[1:]:
        common &= set(it.keys())
        if not common:
            return None
    for key in sorted(common):
        projected = [it.get(key) for it in source]
        if projected == target:
            jq = f"[].{key}" if key.isidentifier() else f"[].{json.dumps(key)}"
            return json_query_expr(jq)
    return None

def detect_filter_by_equality(source, target):
    """selectattr('k','equalto',v) OR json_query('[?k==`v`]')"""
    if not (isinstance(source, list) and isinstance(target, list) and source and target):
        return None
    if not all(isinstance(x, dict) for x in source + target):
        return None
    keys = set().union(*[set(d.keys()) for d in source if isinstance(d, dict)])
    for k in sorted(keys):
        vals = {d.get(k) for d in target}
        if len(vals) == 1:
            v = next(iter(vals))
            filtered = [d for d in source if d.get(k) == v]
            if filtered == target:
                key = k if (isinstance(k, str) and k.isidentifier()) else json.dumps(k)
                val_literal = json.dumps(v)
                jq = f"[?{key}==`{val_literal}`]"
                return json_query_expr(jq)
    return None

# --- Main App --------------------------------------------------------------

class This2That(App):
    CSS = """
    Horizontal { height: 1fr; }
    Tree { width: 40%; border: solid green; }
    TextArea#output_editor { width: 60%; border: solid blue; overflow: auto; }
    TextArea#expr_input { height: 3; border: solid yellow; }
    Input.search-bar { height: 3; border: solid magenta; }
    Static#suggestion_bar { height: 3; border: solid cyan; }
    Static#jmes_tester_popup { width: 100%; height: 12; border: solid white; }
    """

    show_search = reactive(False)
    filter_mode = reactive(False)
    edit_right = reactive(False)
    show_jmes_tester = reactive(False)

    def __init__(self, data_file: str):
        super().__init__()
        self.data_file = data_file
        self.data = None
        self.data_load_error = None
        self.selected_value = None

        # Node and search state
        self.node_map = {}
        self.search_results = []
        self.search_index = 0
        self.search_query = ""

        # AI state
        self.pending_suggestion = None
        self.pending_target_obj = None
        self.last_verification_ok = False

        # JMESPath Tester state
        self.jmes_input = ""
        self.jmes_output = ""

        # Jinja2 + Ansible filters
        self.j2_env = Environment()
        self.j2_env.filters.update(AnsibleFilters().filters())  # includes json_query

    # --- Data Load ---
    def load_data(self):
        try:
            with open(self.data_file, "r") as f:
                content = f.read()
            try:
                return yaml_parser.load(content)
            except Exception:
                return json.loads(content)
        except Exception as e:
            self.data_load_error = str(e)
            return None

    # --- Layout ---
    def compose(self) -> ComposeResult:
        with Vertical():
            with Horizontal():
                # Use id instead of self.tree to avoid collision
                yield Tree("YAML/JSON Data", id="data_tree")
                self.output_editor = TextArea(id="output_editor")
                self.output_editor.read_only = True
                self.output_editor.placeholder = "Live output. Ctrl+E to edit target."
                yield self.output_editor

            self.suggestion_bar = Static(id="suggestion_bar")
            yield self.suggestion_bar

            self.search_input = Input(
                placeholder="Search (Esc=close, Enter=next, Shift+Enter=prev, Ctrl+Shift+F=filter mode)",
                classes="search-bar",
            )
            self.search_input.visible = False
            yield self.search_input

            self.jmes_tester_popup = Static(id="jmes_tester_popup")
            self.jmes_tester_popup.visible = False
            yield self.jmes_tester_popup

            self.expr_input = TextArea(
                placeholder='Enter Jinja2 expression, e.g., {{ selected | json_query("servers[*].name") }}',
                id="expr_input",
            )
            yield self.expr_input

    # --- Tree Build ---
    def on_mount(self):
        self.data_tree = self.query_one("#data_tree", Tree)
        self.data = self.load_data()
        if self.data_load_error:
            self.data_tree.root.set_label("Invalid Input File")
            self.data_tree.root.add_leaf(Text(f"Error parsing file:\n{self.data_load_error}", style="bold red"))
            self.data_tree.root.expand()
            return
        self.build_tree(self.data, self.data_tree.root, path=[])
        self.data_tree.root.expand()
        self.update_suggestion_bar("Ctrl+E edit target • Ctrl+I infer • Ctrl+Y accept • Ctrl+J JMES tester")

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

    # --- Suggestion Bar ---
    def update_suggestion_bar(self, message, ok=None):
        style = "bold green" if ok else ("bold yellow" if ok is None else "bold red")
        self.suggestion_bar.update(Panel(Text(message, style=style), title="Assist"))

    # --- Output render ---
    def refresh_output(self, force=False):
        expr = self.expr_input.text.strip()
        if not expr or self.selected_value is None:
            self.output_editor.text = "Enter a Jinja2 expression below to see results."
            return
        try:
            result = self.evaluate_expression(expr, self.selected_value)
            try:
                obj = yaml_or_json_load(result) if isinstance(result, str) else result
            except Exception:
                obj = result
            self.pretty_update_right(obj)
            if self.pending_target_obj is not None:
                self.verify_pending()
        except TemplateSyntaxError as e:
            self.output_editor.text = f"Jinja2 Syntax Error: {e.message}"
        except RuntimeError as e:
            self.output_editor.text = f"Runtime Error: {e}"

    def evaluate_expression(self, expr, value):
        try:
            template = self.j2_env.from_string(expr)
            return template.render(selected=value)
        except TemplateSyntaxError as e:
            raise TemplateSyntaxError(str(e), e.lineno, e.name, e.filename)
        except Exception as e:
            raise RuntimeError(str(e))

    def pretty_update_right(self, result):
        if isinstance(result, (dict, list)):
            self.output_editor.text = json.dumps(result, indent=2)
            return
        if isinstance(result, str):
            s = result.strip()
            if s.startswith("{") or s.startswith("["):
                try:
                    self.output_editor.text = json.dumps(json.loads(result), indent=2)
                    return
                except json.JSONDecodeError:
                    pass
            self.output_editor.text = result
            return
        try:
            self.output_editor.text = json.dumps(result, indent=2)
        except Exception:
            self.output_editor.text = str(result)

    # --- AI Assist ---
    def infer_filter_from_edit(self):
        if self.selected_value is None:
            self.update_suggestion_bar("Select a node on the left first.", ok=False)
            return
        txt = self.output_editor.text.strip()
        if not txt:
            self.update_suggestion_bar("Right editor is empty. Ctrl+E to paste/type your target.", ok=False)
            return
        try:
            target = yaml_or_json_load(txt)
        except Exception as e:
            self.update_suggestion_bar(f"Target parse error (not YAML/JSON): {e}", ok=False)
            return

        source = deepcopy(self.selected_value)

        # A) exact subpath -> prefer json_query path
        path = find_subnode_path(source, target)
        if path is not None:
            jq = path_to_jmespath(path)
            cand = json_query_expr(jq)
            self.pending_suggestion = cand
            self.pending_target_obj = target
            self.verify_pending()
            return

        # B) projection -> json_query
        cand = detect_projection(source, target)
        if cand:
            self.pending_suggestion = cand
            self.pending_target_obj = target
            self.verify_pending()
            return

        # C) equality filter -> json_query
        cand = detect_filter_by_equality(source, target)
        if cand:
            self.pending_suggestion = cand
            self.pending_target_obj = target
            self.verify_pending()
            return

        self.pending_suggestion = None
        self.pending_target_obj = None
        self.update_suggestion_bar("Couldn't synthesize a json_query for this transformation (yet).", ok=False)

    def verify_pending(self):
        if not self.pending_suggestion or self.pending_target_obj is None:
            self.update_suggestion_bar("Ctrl+E edit target • Ctrl+I infer • Ctrl+Y accept • Ctrl+J JMES tester", ok=None)
            return
        try:
            result = self.evaluate_expression(self.pending_suggestion, self.selected_value)
            try:
                obj = yaml_or_json_load(result) if isinstance(result, str) else result
            except Exception:
                obj = result
            ok = deep_equal(obj, self.pending_target_obj)
            if ok:
                self.update_suggestion_bar(
                    f"✓ json_query matches target: {self.pending_suggestion}   (Ctrl+Y to accept)", ok=True
                )
            else:
                self.update_suggestion_bar(
                    f"Candidate does not fully match target. Suggestion: {self.pending_suggestion}", ok=False
                )
        except Exception as e:
            self.update_suggestion_bar(f"Error verifying suggestion: {e}", ok=False)

    # --- JMES Tester ---
    def toggle_jmes_tester(self, close=False):
        self.show_jmes_tester = not close if not self.show_jmes_tester else (not close)
        if self.show_jmes_tester:
            self.search_input.visible = True
            self.search_input.placeholder = "JMESPath query (Enter=Run, Ctrl+Y=Insert, Esc=Close)"
            self.search_input.value = ""
            self.jmes_input = ""
            self.jmes_output = ""
            self.jmes_tester_popup.visible = True
            self.jmes_tester_popup.update(Panel("Type your JMESPath query and press Enter", title="JMESPath Tester"))
            self.search_input.focus()
        else:
            self.jmes_tester_popup.visible = False
            self.jmes_tester_popup.update("")
            self.search_input.value = ""
            self.search_input.visible = False
            self.show_jmes_tester = False

    def run_jmes_query(self):
        if self.selected_value is None:
            self.jmes_tester_popup.update(Panel("No node selected!", title="JMESPath Tester", border_style="red"))
            return
        try:
            query = self.search_input.value.strip() or "@"
            self.jmes_input = query
            result = jmespath.search(query, self.selected_value)
            self.jmes_output = pretty_json(result)
            content = (
                f"Query: {query}\n\nResult:\n{self.jmes_output}\n\n"
                "Press Esc to close or Ctrl+Y to insert into main expression."
            )
            self.jmes_tester_popup.update(Panel(content, title="JMESPath Tester", border_style="cyan"))
        except Exception as e:
            self.jmes_tester_popup.update(Panel(f"Error: {e}", title="JMESPath Tester", border_style="red"))

# --- main ---
if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python this2that.py <file.yaml|file.json>")
        sys.exit(1)
    app = This2That(sys.argv[1])
    app.run()
