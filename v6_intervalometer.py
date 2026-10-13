#!/usr/bin/env python3
"""Safe relay controller for the Olympus wired remote contacts."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol


class Output(Protocol):
    def on(self) -> None: ...
    def off(self) -> None: ...
    def close(self) -> None: ...


class SimulatedOutput:
    """No-hardware output used by tests and operator dry runs."""

    def __init__(self, pin: int, *, active_high: bool, initial_value: bool):
        self.pin = pin
        self.active_high = active_high
        self.value = bool(initial_value)
        self.closed = False

    def on(self) -> None:
        self.value = True

    def off(self) -> None:
        self.value = False

    def close(self) -> None:
        self.off()
        self.closed = True


def gpiozero_output(pin: int, *, active_low: bool) -> Output:
    try:
        from gpiozero import OutputDevice
    except ImportError as exc:
        raise RuntimeError("gpiozero is required for wired capture on the Raspberry Pi") from exc
    return OutputDevice(pin, active_high=not active_low, initial_value=False)


@dataclass(frozen=True)
class PulseResult:
    started_monotonic: float
    ended_monotonic: float

    @property
    def duration_seconds(self) -> float:
        return self.ended_monotonic - self.started_monotonic


class WiredCamera:
    """Own the isolated AF and release relays and always release them on exit."""

    def __init__(
        self,
        focus_gpio: int,
        shutter_gpio: int,
        *,
        active_low: bool = True,
        simulate: bool = False,
    ) -> None:
        if focus_gpio == shutter_gpio:
            raise ValueError("Focus and shutter GPIOs must be different")
        self.simulate = simulate
        self.focus: Output | None = None
        self.shutter: Output | None = None
        try:
            if simulate:
                self.focus = SimulatedOutput(
                    focus_gpio, active_high=not active_low, initial_value=False
                )
                self.shutter = SimulatedOutput(
                    shutter_gpio, active_high=not active_low, initial_value=False
                )
            else:
                self.focus = gpiozero_output(focus_gpio, active_low=active_low)
                self.shutter = gpiozero_output(shutter_gpio, active_low=active_low)
        except Exception:
            if self.focus is not None:
                self.focus.close()
            if self.shutter is not None:
                self.shutter.close()
            raise
        self.closed = False

    @staticmethod
    def _pulse(output: Output, seconds: float) -> PulseResult:
        if seconds <= 0:
            raise ValueError("Relay pulse duration must be positive")
        started = time.monotonic()
        output.on()
        try:
            time.sleep(seconds)
        finally:
            output.off()
        return PulseResult(started, time.monotonic())

    def autofocus_once(self, seconds: float) -> PulseResult:
        return self._pulse(self.focus, seconds)

    def take_picture(self, seconds: float) -> PulseResult:
        return self._pulse(self.shutter, seconds)

    def focus_and_take_picture(
        self,
        focus_seconds: float,
        shutter_seconds: float,
    ) -> PulseResult:
        """Perform a conventional remote full press with AF held for release."""
        if focus_seconds <= 0 or shutter_seconds <= 0:
            raise ValueError("Relay pulse durations must be positive")
        started = time.monotonic()
        self.focus.on()
        try:
            time.sleep(focus_seconds)
            self.shutter.on()
            try:
                time.sleep(shutter_seconds)
            finally:
                self.shutter.off()
        finally:
            self.focus.off()
        return PulseResult(started, time.monotonic())

    def clear_half_press(self, seconds: float) -> PulseResult:
        """Cycle AF after shutter-only capture to clear the RM-CB2 state."""
        return self._pulse(self.focus, seconds)

    def close(self) -> None:
        if self.closed:
            return
        if self.focus is not None:
            self.focus.off()
        if self.shutter is not None:
            self.shutter.off()
        if self.focus is not None:
            self.focus.close()
        if self.shutter is not None:
            self.shutter.close()
        if not self.simulate:
            # gpiozero's lgpio factory otherwise keeps line reservations alive
            # in long-running dashboard/scheduler processes after devices close.
            from gpiozero import Device

            factory = Device.pin_factory
            if factory is not None:
                factory.close()
                Device.pin_factory = None
        self.closed = True

    def __enter__(self) -> "WiredCamera":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
