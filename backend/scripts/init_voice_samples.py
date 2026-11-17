#!/usr/bin/env python3
"""一次性生成 13 个百炼官方音色的**静态试听样例**。

**为什么是脚本而不是手工上传**：试听样例要跟官方音色表一一对应，
每次升级音色表都得重做一遍；脚本化之后「新增音色 → 跑一条命令 → 试听页
立刻能听」是确定性的，不依赖网盘里的散装音频。

产物（默认写到 ``backend/assets/voice_samples/``）::

    voice_samples/Elias.wav      # 墨讲师（男声旁白）
    voice_samples/Ethan.wav      # 晨煦（年轻男主）
    ...                          # 共 13 个
    voice_samples/manifest.json  # 音色元数据 + 台词 + 时长

**规则**：

* **已存在直接跳过，绝不重复合成**——``--force`` 才覆盖。这样反复跑脚本
  不会白白烧 TTS 额度，也不会把用户换过的样例冲掉。
* 试听样例是**静态音频**：前端只改 ``audio.playbackRate`` 实时变速播放，
  点试听按钮绝不调用 TTS 合成接口。
* 合成后尽量用 ffmpeg 裁掉首尾静音（找不到 ffmpeg 就原样保存）。

用法::

    python backend/scripts/init_voice_samples.py                     # 补齐缺失的样例
    python backend/scripts/init_voice_samples.py --voice Elias        # 只补某个音色
    python backend/scripts/init_voice_samples.py --force              # 全部重生成
    python backend/scripts/init_voice_samples.py --output-dir /tmp/vs
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

#: 允许 ``python backend/scripts/init_voice_samples.py`` 直接运行（补 ``backend/src`` 到 sys.path）。
_BACKEND_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from providers.tts_qwen import BAILIAN_VOICE_CATALOG  # noqa: E402

#: 试听样例目录名（相对 ``backend/assets/``）。
VOICE_SAMPLES_DIR_NAME = "voice_samples"

#: 样例清单文件名。
MANIFEST_FILENAME = "manifest.json"

#: 所有音色共用的试听台词（约 8 秒）。
#:
#: 用**同一句**台词给所有音色，才能直观对比音色差异；台词本身要平实、
#: 不带强烈情绪，否则听感被情绪指令带偏，看不出音色本身的质地。
SAMPLE_TEXT = "夜色落在窗台上，故事才刚刚开始。你听见了吗，风声里藏着的那句话。"

#: 情绪指令（保持克制，避免盖过音色本身）。
SAMPLE_INSTRUCTION = "用自然、平稳、有代入感的语气朗读，不要夸张表演。"


def default_output_dir() -> Path:
    """默认输出目录：``backend/assets/voice_samples/``。"""
    return Path(__file__).resolve().parents[1] / "assets" / VOICE_SAMPLES_DIR_NAME


def _wav_duration_ms(path: Path) -> int:
    """读 WAV 时长（毫秒）；读不出来返回 0。"""
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate() or 1
        return int(frames * 1000 / rate)
    except Exception:  # pragma: no cover - 非 WAV 或损坏文件
        return 0


def _trim_silence(src: Path, *, ffmpeg_bin: str = "ffmpeg") -> bool:
    """裁掉首尾静音；成功返回 ``True``（就地覆盖 src）。"""
    ffmpeg = shutil.which(ffmpeg_bin)
    if not ffmpeg:
        return False
    tmp = src.with_name(f".trim_{src.stem}.wav")
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-af",
        "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0:"
        "detection=peak,areverse,"
        "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.05:"
        "detection=peak,areverse",
        "-c:a",
        "pcm_s16le",
        str(tmp),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=120)
    except Exception:  # pragma: no cover - ffmpeg 异常不应中断批量生成
        tmp.unlink(missing_ok=True)
        return False
    if result.returncode != 0 or not tmp.is_file() or tmp.stat().st_size <= 44:
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(src)
    return True


async def _synthesize_voice(
    client: Any,
    voice_id: str,
    *,
    text: str = SAMPLE_TEXT,
    instruction: str = SAMPLE_INSTRUCTION,
) -> bytes:
    """合成一个音色的样例音频字节。"""
    result = await client.synthesize(text, instruction=instruction, voice=voice_id)
    audio = getattr(result, "audio", b"") or b""
    if not audio:
        raise RuntimeError(f"音色 {voice_id} 的合成结果为空")
    return audio


async def generate_voice_samples(
    output_dir: str | Path | None = None,
    *,
    client: Any | None = None,
    voices: Sequence[str] | None = None,
    force: bool = False,
    trim: bool = True,
    ffmpeg_bin: str = "ffmpeg",
) -> dict[str, Path]:
    """补齐缺失的试听样例，返回 ``{voice_id: wav 路径}``。

    * ``client`` 为 ``None`` 时用百炼官方合成器（需要 ``DASHSCOPE_API_KEY``）。
    * ``voices`` 限定只处理这些音色；``None`` 表示全部 13 个。
    * ``force=False``（默认）时**已存在的样例直接跳过，绝不重复合成**。
    """
    target_dir = Path(output_dir) if output_dir is not None else default_output_dir()
    target_dir.mkdir(parents=True, exist_ok=True)

    wanted = [str(v) for v in voices] if voices else None
    results: dict[str, Path] = {}
    generated: list[str] = []
    skipped: list[str] = []

    owns_client = client is None
    if client is None:
        from providers.tts_qwen import QwenTTS

        client = QwenTTS()

    try:
        for item in BAILIAN_VOICE_CATALOG:
            voice_id = str(item["id"])
            if wanted is not None and voice_id not in wanted:
                continue
            path = target_dir / f"{voice_id}.wav"
            results[voice_id] = path
            if path.is_file() and not force:
                skipped.append(voice_id)
                continue
            audio = await _synthesize_voice(client, voice_id)
            path.write_bytes(audio)
            if trim:
                _trim_silence(path, ffmpeg_bin=ffmpeg_bin)
            generated.append(voice_id)
            print(f"[init_voice_samples] 已生成 {voice_id} → {path.name}")
    finally:
        if owns_client:
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "text": SAMPLE_TEXT,
        "instruction": SAMPLE_INSTRUCTION,
        "voices": {
            str(item["id"]): {
                "id": str(item["id"]),
                "name": str(item["name"]),
                "gender": str(item["gender"]),
                "traits": str(item["traits"]),
                "use_case": str(item["use_case"]),
                "file": f"{item['id']}.wav",
                "duration_ms": _wav_duration_ms(results[str(item["id"])]),
            }
            for item in BAILIAN_VOICE_CATALOG
            if str(item["id"]) in results
        },
    }
    (target_dir / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(
        f"[init_voice_samples] 完成：新生成 {len(generated)} 个，跳过 {len(skipped)} 个 → {target_dir}"
    )
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="生成百炼 13 个官方音色的静态试听样例（已存在则跳过）"
    )
    parser.add_argument("--output-dir", default=None, help="输出目录（默认 backend/assets/voice_samples）")
    parser.add_argument("--voice", action="append", default=None, help="只处理指定音色（可重复）")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的样例（默认跳过）")
    parser.add_argument("--no-trim", action="store_true", help="不裁剪首尾静音")
    args = parser.parse_args(argv)

    try:
        asyncio.run(
            generate_voice_samples(
                args.output_dir,
                voices=args.voice,
                force=args.force,
                trim=not args.no_trim,
            )
        )
    except Exception as exc:
        print(f"[init_voice_samples] 生成失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - 脚本入口
    raise SystemExit(main())
