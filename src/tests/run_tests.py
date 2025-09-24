

import json
import sys
from io import StringIO
from pathlib import Path

from ruamel.yaml import YAML
from jinja2 import Environment, TemplateSyntaxError
import jmespath

# Import the same Ansible filters that this2that uses
def setup_jinja_environment():
    env = Environment()
    try:
        from ansible.plugins.filter.core import FilterModule as AnsibleFilters
        env.filters.update(AnsibleFilters().filters())
        print("[INFO] Loaded Ansible filters:", list(env.filters.keys()))
    except Exception as e:
        print("[WARNING] Failed to load Ansible filters:", e)

    # Ensure json_query exists
    if "json_query" not in env.filters:
        print("[WARNING] json_query filter not found, adding fallback.")
        def json_query(data, expression):
            try:
                return jmespath.search(expression, data)
            except Exception as e:
                print("json_query error:", e)
                return None
        env.filters["json_query"] = json_query

    return env

yaml_parser = YAML(typ="safe")

def yaml_or_json_load(text):
    """Try to parse text as YAML first, then JSON."""
    try:
        return yaml_parser.load(text)
    except Exception:
        return json.loads(text)

def run_single_test(env, test):
    """Run a single test case and return (passed, actual_result)."""
    test_name = test.get("name", "Unnamed Test")
    input_data = test["input"]
    filter_expr = test["filter"]
    expected_output = test["expected_output"]

    # Build full Jinja expression
    if not filter_expr.strip().startswith("{{"):
        filter_expr = "{{ " + filter_expr.strip() + " }}"

    try:
        template = env.from_string(filter_expr)
        rendered = template.render(selected=input_data)

        # Try to parse rendered result as JSON/YAML
        try:
            actual_result = yaml_or_json_load(rendered)
        except Exception:
            actual_result = rendered.strip()

        # Compare results
        passed = actual_result == expected_output
        return passed, actual_result, None

    except TemplateSyntaxError as e:
        return False, None, f"Template Syntax Error: {e.message}"
    except Exception as e:
        return False, None, f"Runtime Error: {str(e)}"

def run_tests(test_file_path):
    # Load test file
    with open(test_file_path, "r") as f:
        test_data = yaml_parser.load(f)

    tests = test_data.get("tests", [])
    env = setup_jinja_environment()

    passed_count = 0
    failed_count = 0

    for idx, test in enumerate(tests, start=1):
        passed, actual, error = run_single_test(env, test)
        test_name = test.get("name", f"Test {idx}")
        print(f"\n[{idx}] {test_name}")

        if passed:
            print("  ✅ PASS")
            passed_count += 1
        else:
            print("  ❌ FAIL")
            failed_count += 1
            if error:
                print("    Error:", error)
            else:
                print("    Expected:", json.dumps(test['expected_output'], indent=2))
                print("    Got:", json.dumps(actual, indent=2))

    print("\n--- TEST SUMMARY ---")
    print(f"Total: {len(tests)}")
    print(f"Passed: {passed_count}")
    print(f"Failed: {failed_count}")

    return failed_count == 0

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python run_tests.py tests.yaml")
        sys.exit(1)

    test_file = sys.argv[1]
    success = run_tests(test_file)
    sys.exit(0 if success else 1)
