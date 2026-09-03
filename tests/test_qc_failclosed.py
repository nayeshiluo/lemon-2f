"""
FFprobe 质检 Fail-Closed 安全回归测试。

历史缺陷：ffprobe 返回非 0 或未安装时，inspect() 会伪造
duration=3600 / 1920x1080 / h264 元数据并返回 is_valid=True。
后果：任意 8MB 随机字节的假文件都能骗过质检，直接换取软妹币。
"""
import os
import asyncio
import pytest

from backend.qc.inspector import ffprobe_qc, FFprobeQCService


@pytest.fixture
def fake_video(tmp_path):
    """构造一个体积足够大、但内容是随机字节的假视频文件"""
    p = tmp_path / "Fake.Show.S01E07.1080p.mkv"
    p.write_bytes(os.urandom(8 * 1024 * 1024))
    return str(p)


@pytest.mark.asyncio
async def test_fake_random_bytes_file_is_rejected(fake_video):
    """核心防刷：随机字节假文件必须被质检拦截，绝不能伪造元数据放行"""
    is_valid, reason, meta = await ffprobe_qc.inspect(fake_video)
    assert is_valid is False, f"假文件竟然通过了质检！reason={reason} meta={meta}"
    assert meta == {}, "被拒绝的文件绝不允许返回伪造元数据"
    assert "QC_PROBE_FAILED" in reason or "视频流" in reason, reason


@pytest.mark.asyncio
async def test_missing_ffprobe_is_fail_closed(fake_video, monkeypatch):
    """ffprobe 未安装时必须 Fail-Closed 拒绝，而不是默认放行"""
    async def _raise_not_found(*args, **kwargs):
        raise FileNotFoundError("ffprobe not installed")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _raise_not_found)

    is_valid, reason, meta = await ffprobe_qc.inspect(fake_video)
    assert is_valid is False, "环境无 ffprobe 时绝不允许放行"
    assert "QC_UNAVAILABLE" in reason, reason
    assert meta == {}


@pytest.mark.asyncio
async def test_nonexistent_file_rejected():
    """不存在的文件必须拒绝"""
    is_valid, reason, meta = await ffprobe_qc.inspect("/tmp/definitely_not_here_9animal.mkv")
    assert is_valid is False
    assert meta == {}


@pytest.mark.asyncio
async def test_real_video_passes_qc(tmp_path):
    """
    正向验证：用 ffmpeg 生成一段真实可解析的视频，必须通过质检并返回真实元数据。
    若环境无 ffmpeg 则跳过（此时 fail-closed 行为已由上面的测试覆盖）。
    """
    out = tmp_path / "Real.Show.S01E07.mkv"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=10",
        "-t", "40", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        str(out),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()
    if proc.returncode != 0 or not out.exists():
        pytest.skip("环境缺少可用 ffmpeg/libx264，跳过正向质检验证")

    is_valid, reason, meta = await ffprobe_qc.inspect(str(out))
    assert is_valid is True, f"真实视频被误拦截: {reason}"
    assert meta["duration_seconds"] >= 30, meta
    assert meta["width"] == 640 and meta["height"] == 360, meta
    assert meta["video_codec"] == "h264", meta
    assert meta["is_4k"] is False


@pytest.mark.asyncio
async def test_short_video_rejected_as_ad(tmp_path):
    """时长过短（疑似广告/预告）必须拦截"""
    out = tmp_path / "Ad.Clip.S01E01.mkv"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
        "-t", "3", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        str(out),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()
    if proc.returncode != 0 or not out.exists():
        pytest.skip("环境缺少可用 ffmpeg/libx264，跳过短视频拦截验证")

    is_valid, reason, meta = await ffprobe_qc.inspect(str(out))
    assert is_valid is False, "3 秒短视频竟通过质检"
    assert "时长过短" in reason, reason


def test_season_episode_parsing_does_not_mistake_resolution():
    """集数解析不得把 1080/2160/年份误判为集数"""
    parse = FFprobeQCService.parse_season_episode_from_filename
    assert parse("Show.S01E07.1080p.mkv") == (1, 7)
    assert parse("Show.EP12.2026.mkv") == (1, 12)
    assert parse("剧名 第152集.mkv") == (1, 152)
    assert parse("[05].mkv") == (1, 5)
    # 纯分辨率/年份文件名不应被解析出集数
    assert parse("Movie.2026.2160p.x265.mkv")[1] not in (2160, 2026, 265)
