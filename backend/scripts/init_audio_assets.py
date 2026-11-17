#!/usr/bin/env python3
"""生成零版权（CC0）氛围 BGM 与音效素材。

**为什么需要脚本而不是直接放二进制**：素材是「程序合成」的波形，不是录音，
所以它天然没有版权问题，可以随仓库分发；用脚本生成还能保证
「任何人 clone 下来都能重建出同一份素材」，不必依赖网盘。

产物（默认写到 ``backend/assets/``）::

    bgm/suspense.wav    # 45s 悬疑垫底
    bgm/emotional.wav   # 45s 情感垫底
    bgm/action.wav      # 45s 动作垫底（备用）
    sfx/heart_thump.wav # 心跳
    sfx/door_slam.wav   # 关门/摔门
    sfx/whoosh.wav      # 呼啸转场
    sfx/gasp.wav        # 倒吸气
    sfx/phone_msg.wav   # 短信提示
    sfx/footstep.wav    # 脚步声
    sfx/crash.wav       # 撞击/碎裂

**规则**：

* **默认不覆盖已有文件**——``--force`` 才覆盖。这样用户自己换过的素材
  不会被脚本冲掉（``--force`` 只覆盖同名文件，不删目录）。
* BGM 是**可无缝循环**的：生成后把末尾 0.5s 与开头 0.5s 做交叉淡化，
  首尾接缝听不出来。
* 采样率 44100Hz / 16bit / 单声道；音效 0.3~1.8s。

用法::

    python backend/scripts/init_audio_assets.py                # 生成到 backend/assets
    python backend/scripts/init_audio_assets.py --output-dir /tmp/assets
    python backend/scripts/init_audio_assets.py --force
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

#: 采样率（Hz）。
SAMPLE_RATE = 44100
#: BGM 时长（秒）。必须与 Stage 7 的默认 BGM 名称一致（``suspense`` / ``emotional``）。
BGM_SECONDS = 45.0
#: 循环交叉淡化时长（秒）。
LOOP_FADE_SECONDS = 0.5

#: BGM 素材名（不含后缀）。
BGM_NAMES: tuple[str, ...] = ("suspense", "emotional", "action")

#: 音效素材名（不含后缀）。
SFX_NAMES: tuple[str, ...] = (
    "heart_thump",
    "door_slam",
    "whoosh",
    "gasp",
    "phone_msg",
    "footstep",
    "crash",
)


def _t(seconds: float) -> np.ndarray:
    """时间轴（秒）。"""
    return np.arange(int(round(seconds * SAMPLE_RATE)), dtype=np.float64) / SAMPLE_RATE


def _loop_safe(freq: float, seconds: float) -> float:
    """把频率吸附到「在时长内刚好跑整数个周期」的频率，保证循环无缝。

    ``45s`` 里跑 ``2000`` 个周期 = 44.444…Hz，首尾相位完全对齐。
    """
    cycles = max(1, int(round(freq * seconds)))
    return cycles / seconds


def _apply_loop_fade(wave: np.ndarray) -> np.ndarray:
    """首尾交叉淡化：末尾 fade 采样与开头 fade 采样叠加，得到无缝循环。"""
    fade = int(LOOP_FADE_SECONDS * SAMPLE_RATE)
    if fade <= 0 or wave.size < fade * 2:
        return wave
    head = wave[:fade].copy()
    tail = wave[-fade:].copy()
    ramp = np.linspace(0.0, 1.0, fade)
    blended = tail * (1.0 - ramp) + head * ramp
    out = wave.copy()
    out[:fade] = blended
    out[-fade:] = out[-fade:]
    return out


def _suspense_bgm(seconds: float = BGM_SECONDS) -> np.ndarray:
    """悬疑垫底：低频 drone + 缓慢起伏 + 极轻的风噪。"""
    t = _t(seconds)
    base = _loop_safe(58.0, seconds)
    drone = 0.55 * np.sin(2 * np.pi * base * t)
    drone += 0.28 * np.sin(2 * np.pi * _loop_safe(87.0, seconds) * t + 0.7)
    drone += 0.16 * np.sin(2 * np.pi * _loop_safe(116.0, seconds) * t + 1.9)
    # 缓慢呼吸感（整数周期调制）。
    lfo = 0.5 + 0.5 * np.sin(2 * np.pi * _loop_safe(1.0 / 15.0, seconds) * t)
    wind = 0.05 * np.random.default_rng(11).standard_normal(t.size)
    signal = drone * (0.55 + 0.45 * lfo) + wind
    signal = _apply_loop_fade(signal)
    return np.clip(signal, -1.0, 1.0)


def _emotional_bgm(seconds: float = BGM_SECONDS) -> np.ndarray:
    """情感垫底：温暖的三度和弦 + 极慢的强弱变化。"""
    t = _t(seconds)
    root = _loop_safe(146.83, seconds)
    chord = (
        0.42 * np.sin(2 * np.pi * root * t)
        + 0.30 * np.sin(2 * np.pi * (root * 1.25) * t + 0.4)
        + 0.22 * np.sin(2 * np.pi * (root * 1.5) * t + 1.2)
        + 0.12 * np.sin(2 * np.pi * (root * 2.0) * t + 2.1)
    )
    lfo = 0.5 + 0.5 * np.sin(2 * np.pi * _loop_safe(1.0 / 22.5, seconds) * t + 0.3)
    signal = chord * (0.6 + 0.4 * lfo)
    signal = _apply_loop_fade(signal)
    return np.clip(signal, -1.0, 1.0)


def _action_bgm(seconds: float = BGM_SECONDS) -> np.ndarray:
    """动作垫底：紧绷的低音脉冲 + 推进感。"""
    t = _t(seconds)
    base = _loop_safe(73.42, seconds)
    pulse = np.sin(2 * np.pi * base * t) * (0.55 + 0.45 * np.sin(2 * np.pi * _loop_safe(2.0, seconds) * t))
    shimmer = 0.14 * np.sin(2 * np.pi * _loop_safe(220.0, seconds) * t + 0.9)
    signal = 0.75 * pulse + shimmer
    signal = _apply_loop_fade(signal)
    return np.clip(signal, -1.0, 1.0)


def _thump(freq: float, seconds: float, decay: float) -> np.ndarray:
    """带指数衰减的低频「咚」。"""
    t = _t(seconds)
    return np.sin(2 * np.pi * freq * t) * np.exp(-t / decay)


def _heart_thump(seconds: float = 1.1) -> np.ndarray:
    """心跳：两下沉闷的咚。"""
    t = _t(seconds)
    out = np.zeros_like(t)
    for offset in (0.0, 0.28):
        local = t - offset
        mask = local >= 0
        body = np.zeros_like(t)
        body[mask] = np.sin(2 * np.pi * 62.0 * local[mask]) * np.exp(-local[mask] / 0.09)
        body[mask] += 0.45 * np.sin(2 * np.pi * 41.0 * local[mask]) * np.exp(-local[mask] / 0.13)
        out += body
    return np.clip(out, -1.0, 1.0) * 0.9


def _door_slam(seconds: float = 0.9) -> np.ndarray:
    """关门/摔门：低频冲击 + 木材爆裂噪声。"""
    t = _t(seconds)
    rng = np.random.default_rng(7)
    noise = rng.standard_normal(t.size)
    crack = noise * np.exp(-t / 0.035)
    body = np.sin(2 * np.pi * 58.0 * t) * np.exp(-t / 0.16)
    body += 0.5 * np.sin(2 * np.pi * 96.0 * t) * np.exp(-t / 0.07)
    return np.clip(crack * 0.55 + body * 0.85, -1.0, 1.0)


def _whoosh(seconds: float = 0.8) -> np.ndarray:
    """呼啸转场：噪声经扫频带通（用一阶差分近似滤波）后的快速起落。"""
    t = _t(seconds)
    rng = np.random.default_rng(19)
    noise = rng.standard_normal(t.size)
    # 中心频率随时间上升：用简单的一阶低通再差分近似扫频。
    cutoff = 0.02 + 0.35 * (t / seconds)
    alpha = np.clip(cutoff, 0.005, 0.9)
    state = 0.0
    filtered = np.empty_like(noise)
    for index in range(noise.size):
        state += alpha[index] * (noise[index] - state)
        filtered[index] = state
    band = np.diff(filtered, prepend=0.0)
    env = np.sin(np.pi * np.clip(t / seconds, 0.0, 1.0)) ** 2
    return np.clip(band * env * 2.4, -1.0, 1.0)


def _gasp(seconds: float = 0.7) -> np.ndarray:
    """倒吸气：窄带噪声快速上扬再收住。"""
    t = _t(seconds)
    rng = np.random.default_rng(23)
    noise = rng.standard_normal(t.size)
    state = 0.0
    filtered = np.empty_like(noise)
    alpha = 0.06 + 0.20 * (t / seconds)
    for index in range(noise.size):
        state += alpha[index] * (noise[index] - state)
        filtered[index] = state
    env = np.sin(np.pi * np.clip(t / seconds, 0.0, 1.0)) ** 1.6
    return np.clip(filtered * env * 2.0, -1.0, 1.0)


def _phone_msg(seconds: float = 0.9) -> np.ndarray:
    """短信提示：两声短促的「叮」。"""
    t = _t(seconds)
    out = np.zeros_like(t)
    for offset, freq in ((0.0, 987.77), (0.22, 1318.51)):
        local = t - offset
        mask = local >= 0
        tone = np.zeros_like(t)
        tone[mask] = np.sin(2 * np.pi * freq * local[mask]) * np.exp(-local[mask] / 0.16)
        out += tone
    return np.clip(out * 0.55, -1.0, 1.0)


def _footstep(seconds: float = 0.35) -> np.ndarray:
    """脚步声：短促的「哒」+ 一点地面回声。"""
    t = _t(seconds)
    rng = np.random.default_rng(31)
    noise = rng.standard_normal(t.size) * np.exp(-t / 0.018)
    body = np.sin(2 * np.pi * 96.0 * t) * np.exp(-t / 0.05)
    return np.clip(noise * 0.5 + body * 0.7, -1.0, 1.0)


def _crash(seconds: float = 1.6) -> np.ndarray:
    """撞击/碎裂：多段碎片噪声 + 低频下坠。"""
    t = _t(seconds)
    rng = np.random.default_rng(43)
    out = np.zeros_like(t)
    for offset, decay, level in ((0.0, 0.08, 0.8), (0.09, 0.05, 0.55), (0.19, 0.04, 0.4), (0.32, 0.03, 0.28)):
        local = t - offset
        mask = local >= 0
        burst = np.zeros_like(t)
        burst[mask] = rng.standard_normal(int(mask.sum())) * np.exp(-local[mask] / decay)
        out += burst * level
    sweep = np.sin(2 * np.pi * (150.0 * np.exp(-t / 0.35)) * t) * np.exp(-t / 0.28) * 0.6
    out += sweep
    return np.clip(out, -1.0, 1.0)


#: 素材名 → 生成函数。
_GENERATORS = {
    "suspense": _suspense_bgm,
    "emotional": _emotional_bgm,
    "action": _action_bgm,
    "heart_thump": _heart_thump,
    "door_slam": _door_slam,
    "whoosh": _whoosh,
    "gasp": _gasp,
    "phone_msg": _phone_msg,
    "footstep": _footstep,
    "crash": _crash,
}


def _write_wave(path: Path, signal: np.ndarray) -> Path:
    """把 [-1, 1] 的浮点波形写成 16bit 单声道 WAV。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.clip(signal, -1.0, 1.0)
    pcm = (data * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm.tobytes())
    return path


def default_output_dir() -> Path:
    """默认输出目录：``backend/assets``。"""
    return Path(__file__).resolve().parents[1] / "assets"


def generate_audio_assets(
    output_dir: str | Path | None = None,
    *,
    force: bool = False,
) -> dict[str, Path]:
    """生成全部素材，返回 ``{相对路径: 绝对路径}``。

    * 已存在的文件**默认跳过**（``force=True`` 才覆盖）；
    * ``output_dir`` 缺省是 ``backend/assets``。
    """
    root = Path(output_dir).expanduser() if output_dir else default_output_dir()
    created: dict[str, Path] = {}
    for name in BGM_NAMES:
        target = root / "bgm" / f"{name}.wav"
        if target.exists() and not force:
            continue
        _write_wave(target, _GENERATORS[name]())
        created[f"bgm/{name}.wav"] = target
    for name in SFX_NAMES:
        target = root / "sfx" / f"{name}.wav"
        if target.exists() and not force:
            continue
        _write_wave(target, _GENERATORS[name]())
        created[f"sfx/{name}.wav"] = target
    return created


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成零版权氛围 BGM 与音效素材")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="输出目录（默认 backend/assets）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="覆盖已存在的同名文件（默认不覆盖）",
    )
    args = parser.parse_args(argv)

    created = generate_audio_assets(args.output_dir, force=args.force)
    if not created:
        print("素材已存在，未覆盖任何文件（需要覆盖请加 --force）")
        return 0
    for relative, path in created.items():
        print(f"已生成 {relative} → {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    sys.exit(main())
