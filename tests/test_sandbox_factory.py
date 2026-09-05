import pytest

from olympdev.isolate import IsolateFactory, Sandbox
from olympdev.isolate.runner import IsolateSandBox


@pytest.mark.slow
def test_sandbox_factory():
    factory = IsolateFactory(3)
    with Sandbox(factory) as sandbox:
        assert sandbox.box_id == 1
    with Sandbox(factory) as sandbox:
        assert sandbox.box_id == 2
        with Sandbox(factory) as sandbox:
            assert sandbox.box_id == 3
    with Sandbox(factory) as sandbox:
        assert sandbox.box_id == 1


@pytest.mark.slow
def test_sandbox_factory_queue_timeout():
    factory = IsolateFactory(1)
    with Sandbox(factory) as sandbox:
        assert sandbox.box_id == 1
        with pytest.raises(TimeoutError, match="sandbox slot"):
            Sandbox(factory, lock_timeout_sec=0.1)


def test_sandbox_factory_returns_slot_if_enter_fails(monkeypatch):
    factory = IsolateFactory(1)

    def boom(self):
        raise RuntimeError("init failed")

    monkeypatch.setattr(IsolateSandBox, "_init_box", boom)
    with pytest.raises(RuntimeError, match="init failed"):
        with Sandbox(factory):
            pass
    assert factory.queue.qsize() == 1


def test_sandbox_count_out_of_range():
    with pytest.raises(ValueError, match="sandbox_count"):
        IsolateFactory(0)
    with pytest.raises(ValueError, match="sandbox_count"):
        IsolateFactory(1000)
