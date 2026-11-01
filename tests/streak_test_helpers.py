"""Load only date-rule functions from main.py; never start Discord or Firebase."""
import ast
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path


source = Path(__file__).resolve().parents[1] / 'main.py'
tree = ast.parse(source.read_text(encoding='utf-8'))
names = {
    'empty_streak', 'read_date', 'normalize_streak', 'validate_streak',
    'advance_streak', 'record_task', 'streak_reminder', 'edit_streak_fields',
}
constants = {'ANCHOR', 'LAST_TASK', 'STREAK_VERSION'}
nodes = []
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in names:
        nodes.append(node)
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in constants:
                nodes.append(node)
exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'))
