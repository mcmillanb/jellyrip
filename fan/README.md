# fanctl: temperature-controlled PWM fan

A 4-pin PWM fan driven from the Pi's hardware PWM on GPIO 18 through sysfs, with
a temperature curve, start-up kick, hysteresis and smoothing. If anything stops
controlling it, the fan runs at full speed.

Install: `sudo bash setup/10-fan.sh`. This copies:

| What | Where |
|---|---|
| Script | `/usr/local/bin/fanctl` |
| Settings | `/etc/fanctl.conf` |
| Service | `/etc/systemd/system/fanctl.service` |
| Boot config | the `fanctl` block in `/boot/firmware/config.txt` |

## Wiring

**Fan:** ARCTIC P12 Pro (120 mm, 4-pin PWM, 12 V), powered from its own 12 V
supply (LM2596 buck). It stops below about 5 % PWM. The tach wire isn't connected.

The fan's PWM input is pulled up inside the fan, so the Pi drives it through an
NPN transistor (ZTX450):

| From | Through | To |
|---|---|---|
| Pi GPIO 18 (pin 12) | 1 kΩ | transistor base |
| transistor base | 10 kΩ | GND |
| transistor emitter | | GND (shared with the Pi and the 12 V supply) |
| transistor collector | | fan PWM (fan pin 4) |

**The signal is inverted.** GPIO high turns the transistor on, which pulls the
fan's PWM line low, so the fan slows down. fanctl works in fan % and sets
`invert = yes`, so the GPIO duty is 100 − fan %. A low, idle or unpowered GPIO
means **full speed**:

- before the service starts;
- after it stops or crashes (`ExecStopPost` runs `fanctl failsafe`);
- after the Pi shuts down.

**Power button:** a momentary button between GPIO 3 (pin 5) and GND (pin 6).
Hold it 5 s to shut down; press it to start a halted Pi. This is set up by
discrip (`[power]` in `/opt/discrip/discrip.toml`, see `docs/setup-guide.md`).
Its LED is on GPIO 17 (pin 11) and GND (pin 9).

## Commands

```
fanctl status            temperature, fan %, mode
sudo fanctl test [secs]  100 / 75 / 50 / 30 / 0 / 100 %, 5 s each, then back
sudo fanctl set 40       manual 40 % until...
sudo fanctl auto         ...back to the curve
journalctl -u fanctl     changes in fan speed, with the temperature
```

## Changing the curve

Edit `/etc/fanctl.conf`, then run `sudo systemctl restart fanctl`.

```
curve = 0:20 45:20 55:30 65:40 72:50 75:100   # temperature C : fan %, linear in between
stop_below = 42                    # once running, the fan only stops below this
```

The `0:20` point keeps the fan running at 20 %, which is silent, however cool the Pi
is. With the curve starting at 45 °C the fan stopped below 42 °C, and at idle (about
43 °C in the case) it started and stopped every 2 minutes. To let it stop again,
remove `0:20`.

- **Below the first point:** the fan stops, but once it's running it keeps the
  lowest step until the temperature drops below `stop_below`.
- **Above the last point:** the fan stays at the last value.
- **Starting from stopped:** it runs at `kick_percent` for `kick_seconds` first.
- **Smoothing:** it changes by at most `max_step` % per reading (`interval` s).
  Manual settings apply immediately.
- **Silent idle:** keep the first point's % at 20 or more. The P12 Pro stops
  below about 5 % and may not start reliably just above that.

## Notes

- `dtoverlay=pwm,pin=18,func=2` uses PWM0. The headphone jack's audio
  (`dtparam=audio=on`) uses the Pi 4's second PWM block on GPIO 40/41, so they
  don't clash. I2S audio HATs do use GPIO 18, so don't combine them with this.
- `chip = auto` finds PWM0 by its address (`fe20c000.pwm`), so the number of the
  `pwmchip` directory in `/sys/class/pwm` doesn't matter.
