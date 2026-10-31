import pytest

from appio.contracts import WindowUnavailableError
from tests.p4.files_tab_support import tab_store, read_files, BOOT, H, S


@pytest.mark.parametrize("left,right,refuses", [
    (2*H,3*H,True),
    (4*H,5*H,False),
], ids=["inside-served", "outside-served"])
def test_overlap_uncertainty_is_intersection_scoped_even_with_zero_bytes(
        tab_store, left, right, refuses):
    db = tab_store(hours=(2,3), app_gaps={},
                   gaps=((left,right,"overlap_uncertain"),))
    db.execute("UPDATE file_sample SET bytes_r=0,bytes_w=0")
    db.commit()

    if refuses:
        with pytest.raises(WindowUnavailableError) as caught:
            read_files((("local",db,"gui"),))
        assert (caught.value.reason, str(caught.value)) == (
            "file_source_unavailable", "File ownership cannot be represented by one store.")
    else:
        result = read_files((("local",db,"gui"),))
        assert result.coverage.served_interval == S
        assert result.coverage.observed_bytes_r == 0


def test_overlapping_owner_epochs_refuse_without_persisted_gap_row(tab_store):
    db = tab_store(hours=(2,3), app_gaps={}, gaps=())
    db.execute("UPDATE file_sample SET bytes_r=0,bytes_w=0")
    db.execute("INSERT INTO collector_interval VALUES('iod',?,2,?,?)",
               (BOOT,2*H,4*H))
    db.commit()

    assert db.execute(
        "SELECT count(*) FROM collector_interval WHERE boot_id=?", (BOOT,)
    ).fetchone()[0] == 2
    assert db.execute(
        "SELECT count(*) FROM gap WHERE reason='overlap_uncertain'"
    ).fetchone()[0] == 0
    with pytest.raises(WindowUnavailableError) as caught:
        read_files((("local",db,"gui"),))
    assert (caught.value.reason, str(caught.value)) == (
        "file_source_unavailable", "File ownership cannot be represented by one store.")
