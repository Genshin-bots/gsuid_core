"""动图收成静图：张数上限、拼帧档位、长边约 300。"""

import io

from PIL import Image

from gsuid_core.ai_core.multimodal.animated_frames import (
    CELL,
    sheet_plan,
    sample_indices,
    render_animation_sheets,
)


def _gif(frame_count: int, size: tuple[int, int] = (80, 40)) -> bytes:
    colors = [(index * 20 % 256, 40, 80) for index in range(frame_count)]
    frames = [Image.new("RGB", size, color) for color in colors]
    buf = io.BytesIO()
    frames[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=40,
        loop=0,
    )
    return buf.getvalue()


def test_sheet_plan_steps_up_then_caps() -> None:
    assert sheet_plan(7) == (7, 1)
    assert sheet_plan(16) == (16, 1)
    assert sheet_plan(17) == (17, 3)
    assert sheet_plan(48) == (48, 3)
    assert sheet_plan(49) == (49, 4)
    assert sheet_plan(64) == (64, 4)
    assert sheet_plan(65) == (65, 5)
    assert sheet_plan(80) == (80, 5)
    assert sheet_plan(200) == (80, 5)


def test_sample_keeps_ends() -> None:
    indices = sample_indices(200, 80)
    assert indices[0] == 0
    assert indices[-1] == 199
    assert len(indices) <= 80


def test_short_gif_is_one_image_per_frame() -> None:
    sheets = render_animation_sheets(_gif(7))
    assert sheets is not None
    assert len(sheets.images) == 7
    with Image.open(io.BytesIO(sheets.images[0])) as image:
        assert max(image.size) == CELL


def test_medium_gif_packs_three_per_sheet() -> None:
    sheets = render_animation_sheets(_gif(20))
    assert sheets is not None
    assert len(sheets.images) == 7
    with Image.open(io.BytesIO(sheets.images[0])) as image:
        assert image.size[0] > CELL * 2


def test_long_gif_stops_at_sixteen_sheets() -> None:
    sheets = render_animation_sheets(_gif(120, size=(20, 20)))
    assert sheets is not None
    assert len(sheets.images) == 16
    assert "等距取了 80 帧" in sheets.note


def test_still_image_is_left_alone() -> None:
    buf = io.BytesIO()
    Image.new("RGB", (30, 30), (1, 2, 3)).save(buf, format="PNG")
    assert render_animation_sheets(buf.getvalue()) is None
