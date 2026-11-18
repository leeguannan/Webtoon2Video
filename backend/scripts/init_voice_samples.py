#!/usr/bin/env python3
"""一次性生成百炼官方音色的**静态试听样例**。

**为什么是脚本而不是手工上传**：试听样例要跟官方音色表一一对应，
每次升级音色表都得重做一遍；脚本化之后「新增音色 → 跑一条命令 → 试听页
立刻能听」是确定性的，不依赖网盘里的散装音频。

Patch v3.3 Patch 15：音色表从 13 个扩充到 34 个（含方言/戏剧音色），并支持
``--tier core`` 只生成高频 14 个；带空格的音色 ID（如 ``Eldric Sage``）按
:func:`providers.tts_qwen.voice_sample_filename` 映射成 ``Eldric_Sage.wav``，
与 ``/api/tts/samples/Eldric%20Sage`` 路由解析到同一文件。

Patch v3.4 Patch 16：新增 MiniMax（``speech-2.8-*``）音色的试听样例，并按
**供应商分目录**存放，避免两套 provider 的同名文件互相覆盖::

    voice_samples/qwen/Elias.wav                       # 百炼
    voice_samples/minimax/Chinese__Mandarin__Radio_Host.wav  # MiniMax

``--provider minimax|qwen|all`` 选择要生成的供应商；不带该参数时保持旧行为
（百炼音色直接写在 ``voice_samples/`` 根目录）。

产物（默认写到 ``backend/assets/voice_samples/``）::

    voice_samples/Elias.wav        # 墨讲师（男声旁白）
    voice_samples/Eldric_Sage.wav  # 贤者（带空格 ID 的安全文件名）
    ...                            # 全量 34 个 / core 层 14 个
    voice_samples/manifest.json    # 音色元数据 + 台词 + 时长

**规则**：

* **已存在直接跳过，绝不重复合成**——``--force`` 才覆盖。这样反复跑脚本
  不会白白烧 TTS 额度，也不会把用户换过的样例冲掉。
* **单条合成失败只告警不中断**：方言/戏剧音色若账号未开通，其余音色照常产出。
* 试听样例是**静态音频**：前端只改 ``audio.playbackRate`` 实时变速播放，
  点试听按钮绝不调用 TTS 合成接口。
* 合成后尽量用 ffmpeg 裁掉首尾静音（找不到 ffmpeg 就原样保存）。

用法::

    python backend/scripts/init_voice_samples.py                     # 补齐全量 34 个
    python backend/scripts/init_voice_samples.py --all               # 同上（显式）
    python backend/scripts/init_voice_samples.py --tier core         # 只补齐高频 14 个
    python backend/scripts/init_voice_samples.py --voice Elias       # 只补某个音色
    python backend/scripts/init_voice_samples.py --force             # 全部重生成
    python backend/scripts/init_voice_samples.py --output-dir /tmp/vs
    python backend/scripts/init_voice_samples.py --provider minimax   # MiniMax 28 个音色
    python backend/scripts/init_voice_samples.py --provider all       # 两家都补齐
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

from providers.tts_minimax import (  # noqa: E402
    MINIMAX_VOICES,
    voice_sample_filename as minimax_voice_sample_filename,
)
from providers.tts_qwen import (  # noqa: E402
    CORE_VOICE_IDS,
    QWEN3_TTS_VOICES,
    voice_sample_filename,
)

#: 试听样例目录名（相对 ``backend/assets/``）。
VOICE_SAMPLES_DIR_NAME = "voice_samples"

#: 样例清单文件名。
MANIFEST_FILENAME = "manifest.json"

#: 供应商（Patch v3.4 Patch 16）：样例按供应商分目录隔离。
PROVIDER_QWEN = "qwen"
PROVIDER_MINIMAX = "minimax"
PROVIDER_ALL = "all"
PROVIDER_CHOICES: tuple[str, ...] = (PROVIDER_QWEN, PROVIDER_MINIMAX, PROVIDER_ALL)

#: 音色分层（CLI ``--tier`` 取值 → 本层包含的 voice_id 序列）。
TIER_ALL = "all"
TIER_CORE = "core"

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


def _tier_voice_ids(tier: str) -> tuple[str, ...]:
    """按分层返回要处理的 voice_id 序列（保持音色表内顺序）。"""
    if tier == TIER_CORE:
        return tuple(voice_id for voice_id in CORE_VOICE_IDS)
    return tuple(str(item["voice_id"]) for item in QWEN3_TTS_VOICES)


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


def _write_manifest(target_dir: Path, provider: str = PROVIDER_QWEN) -> None:
    """按目录内**实际存在**的样例文件重建 manifest（不缩窄、不残留失效条目）。"""
    voices: dict[str, dict[str, Any]] = {}
    if provider == PROVIDER_MINIMAX:
        for item in MINIMAX_VOICES:
            voice_id = str(item["id"])
            path = target_dir / minimax_voice_sample_filename(voice_id)
            if not path.is_file():
                continue
            voices[voice_id] = {
                "voice_id": voice_id,
                "display_name": str(item["name"]),
                "gender": str(item["gender"]),
                "style": str(item["style"]),
                "recommended_for": str(item["recommended_for"]),
                "file": path.name,
                "duration_ms": _wav_duration_ms(path),
            }
    else:
        for item in QWEN3_TTS_VOICES:
            voice_id = str(item["voice_id"])
            path = target_dir / voice_sample_filename(voice_id)
            if not path.is_file():
                continue
            voices[voice_id] = {
                "voice_id": voice_id,
                "display_name": str(item["display_name"]),
                "gender": str(item["gender"]),
                "age_group": str(item["age_group"]),
                "category": str(item["category"]),
                "description": str(item["description"]),
                "file": path.name,
                "duration_ms": _wav_duration_ms(path),
            }
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": provider,
        "text": SAMPLE_TEXT,
        "instruction": SAMPLE_INSTRUCTION,
        "voices": voices,
    }
    (target_dir / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


async def _generate_provider_samples(
    target_dir: Path,
    *,
    provider: str,
    client: Any | None,
    voices: Sequence[str] | None,
    tier: str,
    force: bool,
    trim: bool,
    ffmpeg_bin: str,
) -> dict[str, Path]:
    """在 ``target_dir`` 下补齐**某个供应商**的试听样例。"""
    target_dir.mkdir(parents=True, exist_ok=True)

    wanted = {str(v) for v in voices} if voices else None
    if provider == PROVIDER_MINIMAX:
        tier_ids = tuple(str(item["id"]) for item in MINIMAX_VOICES)
        filename_of = minimax_voice_sample_filename
    else:
        tier_ids = _tier_voice_ids(tier)
        filename_of = voice_sample_filename

    results: dict[str, Path] = {}
    generated: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []

    owns_client = client is None
    if client is None:
        if provider == PROVIDER_MINIMAX:
            from core.config import resolve_minimax_api_key_with_env_file

            try:
                resolve_minimax_api_key_with_env_file()
            except Exception as exc:  # noqa: BLE001 - 缺密钥时给一句人话
                raise RuntimeError(
                    f"生成 MiniMax 试听样例需要环境变量 MINIMAX_API_KEY：{exc}"
                ) from exc
            from providers.tts_minimax import MiniMaxTTS

            client = MiniMaxTTS()
        else:
            from providers.tts_qwen import QwenTTS

            client = QwenTTS()

    try:
        for voice_id in tier_ids:
            if wanted is not None and voice_id not in wanted:
                continue
            path = target_dir / filename_of(voice_id)
            if path.is_file() and not force:
                skipped.append(voice_id)
                results[voice_id] = path
                continue
            try:
                audio = await _synthesize_voice(client, voice_id)
                path.write_bytes(audio)
                if trim:
                    _trim_silence(path, ffmpeg_bin=ffmpeg_bin)
            except Exception as exc:  # noqa: BLE001 - 单条失败不中断整批
                failed.append(voice_id)
                print(
                    f"[init_voice_samples] 告警：{voice_id} 合成失败，已跳过："
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                path.unlink(missing_ok=True)
                continue
            generated.append(voice_id)
            results[voice_id] = path
            print(f"[init_voice_samples] 已生成 {voice_id} → {path.name}")
    finally:
        if owns_client:
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()

    # manifest 基于目录内实际存在的样例重建，core 层运行不会抹掉全量条目。
    _write_manifest(target_dir, provider)

    failed_note = f"，失败 {len(failed)} 个（{', '.join(failed)}）" if failed else ""
    print(
        f"[init_voice_samples] 完成：新生成 {len(generated)} 个，跳过 {len(skipped)} 个"
        f"{failed_note} → {target_dir}"
    )
    return results


async def generate_voice_samples(
    output_dir: str | Path | None = None,
    *,
    client: Any | None = None,
    voices: Sequence[str] | None = None,
    tier: str = TIER_ALL,
    force: bool = False,
    trim: bool = True,
    ffmpeg_bin: str = "ffmpeg",
    provider: str | None = None,
    minimax_client: Any | None = None,
) -> dict[str, Path]:
    """补齐缺失的试听样例，返回 ``{voice_id: wav 路径}``（仅成功/已存在的）。

    * ``client`` 为 ``None`` 时用百炼官方合成器（需要 ``DASHSCOPE_API_KEY``）。
    * ``tier="all"``（默认）处理全量 34 个；``tier="core"`` 只处理高频 14 个。
    * ``voices`` 在分层结果上再做白名单过滤；``None`` 表示该层全部。
    * ``force=False``（默认）时**已存在的样例直接跳过，绝不重复合成**。
    * 单个音色合成失败只打印告警并跳过，不中断整批；manifest 始终反映目录现状。
    * ``provider``（Patch v3.4 Patch 16）：``None`` 保持旧行为（百炼音色直接写在
      ``output_dir`` 根目录）；``"qwen"`` / ``"minimax"`` 写到 ``output_dir/{provider}/``；
      ``"all"`` 两家都补齐（MiniMax 需要 ``MINIMAX_API_KEY``）。
    """
    if tier not in (TIER_ALL, TIER_CORE):
        raise ValueError(f"未知音色分层：{tier!r}（可选 {TIER_ALL!r}/{TIER_CORE!r}）")
    base_dir = Path(output_dir) if output_dir is not None else default_output_dir()

    if provider is None:
        return await _generate_provider_samples(
            base_dir,
            provider=PROVIDER_QWEN,
            client=client,
            voices=voices,
            tier=tier,
            force=force,
            trim=trim,
            ffmpeg_bin=ffmpeg_bin,
        )
    if provider not in PROVIDER_CHOICES:
        raise ValueError(
            f"未知供应商：{provider!r}（可选 {'/'.join(PROVIDER_CHOICES)}）"
        )
    if provider == PROVIDER_ALL:
        results = await _generate_provider_samples(
            base_dir / PROVIDER_QWEN,
            provider=PROVIDER_QWEN,
            client=client,
            voices=voices,
            tier=tier,
            force=force,
            trim=trim,
            ffmpeg_bin=ffmpeg_bin,
        )
        results.update(
            await _generate_provider_samples(
                base_dir / PROVIDER_MINIMAX,
                provider=PROVIDER_MINIMAX,
                client=minimax_client,
                voices=voices,
                tier=tier,
                force=force,
                trim=trim,
                ffmpeg_bin=ffmpeg_bin,
            )
        )
        return results
    return await _generate_provider_samples(
        base_dir / provider,
        provider=provider,
        client=client,
        voices=voices,
        tier=tier,
        force=force,
        trim=trim,
        ffmpeg_bin=ffmpeg_bin,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="生成百炼 34 个官方音色的静态试听样例（已存在则跳过）"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="生成全量 34 个音色（默认行为，与 --tier all 等价）",
    )
    parser.add_argument(
        "--tier",
        choices=(TIER_ALL, TIER_CORE),
        default=TIER_ALL,
        help="音色分层：all=全量 34 个（默认），core=高频 14 个",
    )
    parser.add_argument("--output-dir", default=None, help="输出目录（默认 backend/assets/voice_samples）")
    parser.add_argument(
        "--provider",
        choices=PROVIDER_CHOICES,
        default=None,
        help=(
            "供应商（Patch v3.4 Patch 16）：qwen=百炼、minimax=MiniMax、all=两家都补齐；"
            "不带该参数时保持旧行为（百炼音色写在输出目录根）"
        ),
    )
    parser.add_argument("--voice", action="append", default=None, help="只处理指定音色（可重复）")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的样例（默认跳过）")
    parser.add_argument("--no-trim", action="store_true", help="不裁剪首尾静音")
    args = parser.parse_args(argv)

    tier = TIER_ALL if args.all else args.tier
    try:
        asyncio.run(
            generate_voice_samples(
                args.output_dir,
                voices=args.voice,
                tier=tier,
                force=args.force,
                trim=not args.no_trim,
                provider=args.provider,
            )
        )
    except Exception as exc:
        print(f"[init_voice_samples] 生成失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - 脚本入口
    raise SystemExit(main())
