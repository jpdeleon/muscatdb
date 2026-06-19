from muscat_db.transit_fit import validate_fit_options


def test_duration_basis_parameters_can_be_fixed():
    options = {
        "planets": "b",
        "fixed": ["t0", "period", "dur", "u_star", "b", "ror"],
    }

    assert validate_fit_options(options) is None


def test_unsupported_orbital_parameters_cannot_be_fixed():
    for parameter in ("ecc", "omega"):
        error = validate_fit_options({"planets": "b", "fixed": [parameter]})

        assert error is not None
        assert "unknown fixed parameter" in error

