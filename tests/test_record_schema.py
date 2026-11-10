import pytest

from george import record_schema


def _stock_type_input(**overrides):
    base = {
        "name": "Stock de empanadas",
        "mode": "snapshot",
        "fields": [
            {"key": "sabor", "type": "choice", "choices": ["carne", "pollo"], "required": True},
            {"key": "cantidad", "type": "number", "required": True},
        ],
        "measure_field": "cantidad",
        "group_field": "sabor",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# slugify_key
# ---------------------------------------------------------------------------
def test_slugify_key_lowercases_and_replaces_punctuation():
    assert record_schema.slugify_key("Stock de Empanadas!") == "stock_de_empanadas"


def test_slugify_key_prefixes_when_starting_with_digit():
    assert record_schema.slugify_key("2026 ventas").startswith("x_")


def test_slugify_key_truncates_to_40_chars():
    assert len(record_schema.slugify_key("a" * 100)) == 40


# ---------------------------------------------------------------------------
# validate_type_definition — happy paths
# ---------------------------------------------------------------------------
def test_validate_type_definition_happy_path():
    type_def = record_schema.validate_type_definition(_stock_type_input())
    assert type_def["typeKey"] == "stock_de_empanadas"
    assert type_def["mode"] == "snapshot"
    assert type_def["measureField"] == "cantidad"
    assert type_def["groupField"] == "sabor"
    assert type_def["clientLink"] == "none"


def test_validate_type_definition_generates_type_key_from_name():
    type_def = record_schema.validate_type_definition(_stock_type_input())
    assert type_def["typeKey"] == record_schema.slugify_key("Stock de empanadas")


def test_validate_type_definition_without_group_field():
    input_ = _stock_type_input()
    del input_["group_field"]
    type_def = record_schema.validate_type_definition(input_)
    assert type_def["groupField"] is None


def test_validate_type_definition_forces_measure_field_required():
    input_ = _stock_type_input()
    input_["fields"][1]["required"] = False  # cantidad, the measure field
    type_def = record_schema.validate_type_definition(input_)
    measure_field = next(f for f in type_def["fields"] if f["key"] == "cantidad")
    assert measure_field["required"] is True


def test_validate_type_definition_forces_group_field_required():
    input_ = _stock_type_input()
    input_["fields"][0]["required"] = False  # sabor, the group field
    type_def = record_schema.validate_type_definition(input_)
    group_field = next(f for f in type_def["fields"] if f["key"] == "sabor")
    assert group_field["required"] is True


# ---------------------------------------------------------------------------
# validate_type_definition — validation errors
# ---------------------------------------------------------------------------
def test_validate_type_definition_rejects_missing_name():
    with pytest.raises(ValueError):
        record_schema.validate_type_definition(_stock_type_input(name=""))


def test_validate_type_definition_rejects_invalid_explicit_type_key():
    with pytest.raises(ValueError):
        record_schema.validate_type_definition(_stock_type_input(type_key="Not Valid!"))


def test_validate_type_definition_rejects_invalid_mode():
    with pytest.raises(ValueError):
        record_schema.validate_type_definition(_stock_type_input(mode="weekly"))


def test_validate_type_definition_rejects_empty_fields():
    with pytest.raises(ValueError):
        record_schema.validate_type_definition(_stock_type_input(fields=[]))


def test_validate_type_definition_rejects_too_many_fields():
    fields = [{"key": f"f{i}", "type": "text"} for i in range(record_schema.MAX_FIELDS + 1)]
    with pytest.raises(ValueError):
        record_schema.validate_type_definition(
            _stock_type_input(fields=fields, measure_field="f0", group_field=None)
        )


def test_validate_type_definition_rejects_duplicate_field_keys():
    input_ = _stock_type_input()
    input_["fields"].append({"key": "sabor", "type": "text"})
    with pytest.raises(ValueError):
        record_schema.validate_type_definition(input_)


def test_validate_type_definition_rejects_unknown_field_type():
    input_ = _stock_type_input()
    input_["fields"][0]["type"] = "currency"
    with pytest.raises(ValueError):
        record_schema.validate_type_definition(input_)


def test_validate_type_definition_rejects_choice_field_without_choices():
    input_ = _stock_type_input()
    del input_["fields"][0]["choices"]
    with pytest.raises(ValueError):
        record_schema.validate_type_definition(input_)


def test_validate_type_definition_rejects_missing_measure_field():
    input_ = _stock_type_input()
    del input_["measure_field"]
    with pytest.raises(ValueError):
        record_schema.validate_type_definition(input_)


def test_validate_type_definition_rejects_measure_field_not_declared():
    with pytest.raises(ValueError):
        record_schema.validate_type_definition(_stock_type_input(measure_field="no_existe"))


def test_validate_type_definition_rejects_non_numeric_measure_field():
    with pytest.raises(ValueError):
        record_schema.validate_type_definition(_stock_type_input(measure_field="sabor"))


def test_validate_type_definition_rejects_group_field_not_declared():
    with pytest.raises(ValueError):
        record_schema.validate_type_definition(_stock_type_input(group_field="no_existe"))


def test_validate_type_definition_rejects_invalid_client_link():
    with pytest.raises(ValueError):
        record_schema.validate_type_definition(_stock_type_input(client_link="sometimes"))


# ---------------------------------------------------------------------------
# derive_period
# ---------------------------------------------------------------------------
def test_derive_period_extracts_year_month():
    assert record_schema.derive_period("2026-09-12T14:00:00Z") == "2026-09"


# ---------------------------------------------------------------------------
# coerce_values
# ---------------------------------------------------------------------------
def test_coerce_values_happy_path_computes_amount_and_group_key():
    type_def = record_schema.validate_type_definition(_stock_type_input())
    values, amount, group_key, occurred_at, period = record_schema.coerce_values(
        type_def, {"sabor": "carne", "cantidad": 40}, "2026-09-12T08:00:00Z"
    )
    assert values == {"sabor": "carne", "cantidad": 40.0}
    assert amount == 40.0
    assert group_key == "carne"
    assert occurred_at == "2026-09-12T08:00:00Z"
    assert period == "2026-09"


def test_coerce_values_defaults_occurred_at_to_now():
    type_def = record_schema.validate_type_definition(_stock_type_input())
    _, _, _, occurred_at, _ = record_schema.coerce_values(type_def, {"sabor": "carne", "cantidad": 40})
    assert occurred_at  # a real ISO string was generated, not left empty


def test_coerce_values_without_group_field_uses_total_bucket():
    input_ = _stock_type_input()
    del input_["group_field"]
    type_def = record_schema.validate_type_definition(input_)
    _, _, group_key, _, _ = record_schema.coerce_values(type_def, {"sabor": "carne", "cantidad": 40})
    assert group_key == "total"


def test_coerce_values_rejects_unknown_field():
    type_def = record_schema.validate_type_definition(_stock_type_input())
    with pytest.raises(ValueError):
        record_schema.coerce_values(type_def, {"sabor": "carne", "cantidad": 40, "extra": "x"})


def test_coerce_values_rejects_missing_required_field():
    type_def = record_schema.validate_type_definition(_stock_type_input())
    with pytest.raises(ValueError):
        record_schema.coerce_values(type_def, {"sabor": "carne"})


def test_coerce_values_rejects_non_numeric_number_field():
    type_def = record_schema.validate_type_definition(_stock_type_input())
    with pytest.raises(ValueError):
        record_schema.coerce_values(type_def, {"sabor": "carne", "cantidad": "muchas"})


def test_coerce_values_rejects_invalid_choice():
    type_def = record_schema.validate_type_definition(_stock_type_input())
    with pytest.raises(ValueError):
        record_schema.coerce_values(type_def, {"sabor": "queso", "cantidad": 10})


def test_coerce_values_rejects_invalid_boolean():
    input_ = _stock_type_input()
    input_["fields"].append({"key": "activo", "type": "boolean", "required": True})
    type_def = record_schema.validate_type_definition(input_)
    with pytest.raises(ValueError):
        record_schema.coerce_values(type_def, {"sabor": "carne", "cantidad": 10, "activo": "si"})


def test_coerce_values_rejects_invalid_date():
    input_ = _stock_type_input()
    input_["fields"].append({"key": "vence", "type": "date", "required": True})
    type_def = record_schema.validate_type_definition(input_)
    with pytest.raises(ValueError):
        record_schema.coerce_values(type_def, {"sabor": "carne", "cantidad": 10, "vence": "no es una fecha"})


def test_coerce_values_rejects_invalid_occurred_at():
    type_def = record_schema.validate_type_definition(_stock_type_input())
    with pytest.raises(ValueError):
        record_schema.coerce_values(type_def, {"sabor": "carne", "cantidad": 10}, "no es una fecha")


# ---------------------------------------------------------------------------
# validate_query (dynamic group_by / filters / date_bucket — the injection boundary)
# ---------------------------------------------------------------------------
def _type_def():
    return record_schema.validate_type_definition(
        _stock_type_input(
            fields=[
                {"key": "sabor", "type": "choice", "choices": ["carne", "pollo"], "required": True},
                {"key": "cantidad", "type": "number", "required": True},
                {"key": "fecha_compra", "type": "date"},
                {"key": "fecha_entrega", "type": "date"},
            ]
        )
    )


def _f(field, value, op="eq"):
    return {"field": field, "op": op, "value": value}


def test_validate_query_defaults():
    assert record_schema.validate_query(_type_def(), None, None) == (None, None, [])


def test_validate_query_legacy_dict_is_equality():
    _, _, conds = record_schema.validate_query(_type_def(), None, {"sabor": "carne", "cantidad": "5"})
    assert [(c["key"], c["op"], c["value"]) for c in conds] == [("sabor", "=", "carne"), ("cantidad", "=", 5.0)]


def test_validate_query_number_and_date_ranges_on_several_date_fields():
    _, _, conds = record_schema.validate_query(
        _type_def(),
        None,
        [
            _f("cantidad", 10, "gte"),
            _f("cantidad", "20", "lt"),
            _f("fecha_compra", "2026-10-01", "gte"),
            _f("fecha_entrega", "2026-11-05T10:00:00-05:00", "lte"),
        ],
    )
    assert [c["op"] for c in conds] == [">=", "<", ">=", "<="]
    assert conds[1]["value"] == 20.0
    assert conds[2]["day"] is True  # bare date -> calendar-day comparison
    assert conds[3]["day"] is False  # has a time -> full comparison


@pytest.mark.parametrize("field", ["sabor", "fecha_compra"])
def test_validate_query_order_ops_rejected_on_text_and_choice(field):
    if field == "fecha_compra":
        record_schema.validate_query(_type_def(), None, [_f(field, "2026-10-01", "gt")])  # allowed
        return
    with pytest.raises(ValueError):
        record_schema.validate_query(_type_def(), None, [_f(field, "carne", "gt")])


def test_validate_query_rejects_bad_op_value_and_unknown_field():
    with pytest.raises(ValueError):
        record_schema.validate_query(_type_def(), None, [_f("cantidad", 1, "like")])
    with pytest.raises(ValueError):
        record_schema.validate_query(_type_def(), None, [_f("cantidad", "mucho", "gt")])
    with pytest.raises(ValueError):
        record_schema.validate_query(_type_def(), None, [_f("fecha_compra", "ayer", "gte")])
    with pytest.raises(ValueError):
        record_schema.validate_query(_type_def(), None, [{"field": "cantidad", "op": "gt"}])
    with pytest.raises(ValueError):
        record_schema.validate_query(_type_def(), None, [_f("cantidad", 1)] * 11)
    with pytest.raises(ValueError):
        record_schema.validate_query(_type_def(), None, "sabor=carne")


@pytest.mark.parametrize("bad", ['sabor"] OR 1=1 --', "nope", "id"])
def test_validate_query_rejects_undeclared_filter_key(bad):
    with pytest.raises(ValueError):
        record_schema.validate_query(_type_def(), None, [_f(bad, "x")])


@pytest.mark.parametrize("bad", ['sabor"] OR 1=1 --', "values", "c.id", "SABOR", "nope"])
def test_validate_query_rejects_undeclared_group_by(bad):
    with pytest.raises(ValueError):
        record_schema.validate_query(_type_def(), bad, None)


def test_validate_query_date_bucket():
    assert record_schema.validate_query(_type_def(), "fecha_compra", None)[:2] == ("fecha_compra", 7)
    assert record_schema.validate_query(_type_def(), "fecha_entrega", None, "day")[1] == 10
    assert record_schema.validate_query(_type_def(), "fecha_compra", None, "year")[1] == 4
    assert record_schema.validate_query(_type_def(), "sabor", None, "day")[1] is None  # not a date field
    assert record_schema.validate_query(_type_def(), "period", None)[1] is None
    with pytest.raises(ValueError):
        record_schema.validate_query(_type_def(), "fecha_compra", None, "week")
