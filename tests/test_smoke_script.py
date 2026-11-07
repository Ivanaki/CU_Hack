"""Сквозная проверка боевого стенда обязана быть честной сама.

Проверка, спрятанная внутрь `if`, при неудаче предыдущего шага не
выполняется и в отчёт не попадает вовсе — а сводка при этом пишет
«пропущено: 0». Глазами такое не ловится: надо сравнивать список в
выводе со списком в коде. Здесь это делает тест.
"""

from __future__ import annotations

import ast
from pathlib import Path

SMOKE = Path(__file__).resolve().parent.parent / "scripts" / "smoke.py"

# Ради этих проверок задача и делалась: опознание по личной ссылке и то,
# что tg_id из запроса больше никому ничего не засчитывает.
ГЛАВНЫЕ = (
    "чужой ключ загрузки отвергнут",
    "tg_id без служебного ключа игнорируется",
    "личный файл закрыт от посторонних",
)


def _main() -> ast.FunctionDef:
    tree = ast.parse(SMOKE.read_text(encoding="utf-8"))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )


def _check_names(nodes: list[ast.stmt]) -> set[str]:
    """Имена проверок, вызванных в этих ветках кода."""
    found: set[str] = set()
    for node in nodes:
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "check"
                and inner.args
                and isinstance(inner.args[0], ast.Constant)
            ):
                found.add(str(inner.args[0].value))
    return found


def _stored_block() -> ast.If:
    return next(
        node
        for node in ast.walk(_main())
        if isinstance(node, ast.If) and ast.unparse(node.test) == "stored"
    )


def test_проверки_опознания_не_спрятаны_за_чужой_неудачей():
    """На боевом адресе они не отработали ни разу в двух прогонах подряд."""
    guarded = _check_names(_stored_block().body)

    for name in ГЛАВНЫЕ:
        assert name not in guarded, f"проверка «{name}» молча пропадёт из отчёта"


def test_у_каждой_условной_проверки_есть_ветка_пропуска():
    """Пропуск обязан быть видимым: молча исчезнувшая проверка читается как пройденная."""
    block = _stored_block()
    guarded = _check_names(block.body)
    skipped = {
        node.value
        for node in ast.walk(ast.Module(body=block.orelse, type_ignores=[]))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    assert guarded, "блок, зависящий от имени файла, не должен быть пустым"
    assert guarded <= skipped, f"без отметки SKIP останутся: {sorted(guarded - skipped)}"


def test_проверки_опознания_вообще_есть_в_скрипте():
    everything = _check_names(_main().body)

    for name in ГЛАВНЫЕ:
        assert name in everything
