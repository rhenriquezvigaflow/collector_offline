from collector_offline.domain.value_codec import discrete_value, is_discrete, to_storage_value


def test_value_codec_matches_central_storage_semantics() -> None:
    assert to_storage_value(True, "RUN").value_bool is True
    assert to_storage_value(2, "P001_ST").state == 2
    assert to_storage_value(2, "COUNT_INT").value_num == 2.0
    assert to_storage_value(7.25, "PRESSURE").value_num == 7.25
    assert to_storage_value("unsupported", "TEXT").has_value() is False


def test_discrete_detection_does_not_treat_numeric_suffix_as_state() -> None:
    assert is_discrete(1, "P001_ST") is True
    assert discrete_value(1, "P001_ST") == 1
    assert is_discrete(1, "COUNTER_INT") is False
