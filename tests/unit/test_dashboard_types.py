"""The dashboard's API types are generated from the API's response models (one source).

apps/dashboard/src/dto.ts is written by scripts/gen_dashboard_types.py from apps/api/dto.py.
If a response model changes and the file is not regenerated, the dashboard would read a shape
the server no longer sends: this test fails first.
"""

from __future__ import annotations

from scripts.gen_dashboard_types import OUT, generate


def test_the_dashboards_api_types_are_generated_from_the_response_models() -> None:
    assert OUT.read_text(encoding="utf-8") == generate(), (
        "apps/dashboard/src/dto.ts is out of date: run "
        ".venv/bin/python scripts/gen_dashboard_types.py"
    )


def test_every_response_model_has_an_interface() -> None:
    from pydantic import BaseModel

    from apps.api import dto

    text = generate()
    for name, value in vars(dto).items():
        if (
            isinstance(value, type)
            and issubclass(value, BaseModel)
            and value.__module__ == dto.__name__
        ):
            assert f"export interface {name} " in text, name
