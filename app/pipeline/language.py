from __future__ import annotations


def is_chinese(text: str) -> bool:
    visible = [character for character in text if not character.isspace()]
    if not visible:
        return False
    chinese = sum("\u4e00" <= character <= "\u9fff" for character in visible)
    return chinese / len(visible) > 0.30

