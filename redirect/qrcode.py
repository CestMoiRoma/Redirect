"""QR codes as SVG. segno computes the matrix; the drawing is Redirect's, so every part can take its own style.

Coordinates are in modules: the viewBox is the matrix plus its margin, and the image scales without loss.
"""

from dataclasses import dataclass

import segno

from .models import QRDesign

# Pixels per module in the width and height of the SVG (it scales anyway: they only set a default size)
PIXELS_PER_MODULE = 10
EYE = 7  # side of a finder pattern ("eye"), in modules


@dataclass(frozen=True)
class Layout:
    """What gets drawn: the side of the matrix, its dark data modules, and the square kept for the logo."""

    size: int
    modules: frozenset[tuple[int, int]]
    eyes: tuple[tuple[int, int], ...]
    # (x, y, side) of the area cleared for the logo, in matrix modules; None without a logo
    logo_area: tuple[int, int, int] | None


def _in_eye(x: int, y: int, size: int) -> bool:
    return (x < EYE and y < EYE) or (x >= size - EYE and y < EYE) or (x < EYE and y >= size - EYE)


def layout(data: str, design: QRDesign) -> Layout:
    # A logo hides modules: the highest error correction (30 %) makes up for them
    qr = segno.make(data, error="h" if design.logo else "m", micro=False, boost_error=False)
    matrix = qr.matrix
    size = len(matrix)
    logo_area = None
    if design.logo:
        side = round(size * design.logo_size)
        side += (size - side) % 2  # centered on whole modules
        start = (size - side) // 2
        logo_area = (start, start, side)
    modules = frozenset(
        (x, y)
        for y, row in enumerate(matrix)
        for x, dark in enumerate(row)
        if dark
        and not _in_eye(x, y, size)
        and not (
            logo_area
            and logo_area[0] <= x < logo_area[0] + logo_area[2]
            and logo_area[1] <= y < logo_area[1] + logo_area[2]
        )
    )
    eyes = ((0, 0), (size - EYE, 0), (0, size - EYE))
    return Layout(size, modules, eyes, logo_area)


def _number(value: float) -> str:
    """Short, stable numbers: the same design always gives the same bytes."""
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _rounded_rect(x: float, y: float, side: float, radius: float) -> str:
    """A square with rounded corners, as path commands (clockwise)."""
    r, s, n = radius, side, _number
    if r == 0:
        return f"M{n(x)} {n(y)}h{n(s)}v{n(s)}h-{n(s)}z"
    return (
        f"M{n(x + r)} {n(y)}h{n(s - 2 * r)}a{n(r)} {n(r)} 0 0 1 {n(r)} {n(r)}v{n(s - 2 * r)}"
        f"a{n(r)} {n(r)} 0 0 1 -{n(r)} {n(r)}h-{n(s - 2 * r)}a{n(r)} {n(r)} 0 0 1 -{n(r)} -{n(r)}"
        f"v-{n(s - 2 * r)}a{n(r)} {n(r)} 0 0 1 {n(r)} -{n(r)}z"
    )


def _eye(x: int, y: int, style: str) -> str:
    """One finder pattern: a 7x7 ring (outer square minus a 5x5 hole) and a 3x3 center, as one even-odd path."""
    if style == "circle":
        cx, cy = x + 3.5, y + 3.5

        def circle(r: float) -> str:
            return f"M{_number(cx - r)} {_number(cy)}a{_number(r)} {_number(r)} 0 1 0 {_number(2 * r)} 0a{_number(r)} {_number(r)} 0 1 0 -{_number(2 * r)} 0z"

        return circle(3.5) + circle(2.5) + circle(1.5)
    radius = {"square": (0, 0, 0), "rounded": (2, 1.4, 0.9)}[style]
    return (
        _rounded_rect(x, y, 7, radius[0])
        + _rounded_rect(x + 1, y + 1, 5, radius[1])
        + _rounded_rect(x + 2, y + 2, 3, radius[2])
    )


def _modules(cells: frozenset[tuple[int, int]], style: str) -> str:
    if style == "square":
        # Runs of dark modules on a row become one rectangle
        runs, commands = sorted(cells, key=lambda cell: (cell[1], cell[0])), []
        index = 0
        while index < len(runs):
            x, y = runs[index]
            length = 1
            while index + length < len(runs) and runs[index + length] == (x + length, y):
                length += 1
            commands.append(f"M{x} {y}h{length}v1h-{length}z")
            index += length
        return f'<path d="{"".join(commands)}"/>'
    if style == "rounded":
        return "".join(f'<rect x="{x}.05" y="{y}.05" width=".9" height=".9" rx=".3"/>' for x, y in sorted(cells))
    return "".join(f'<circle cx="{x}.5" cy="{y}.5" r=".45"/>' for x, y in sorted(cells))


def render_svg(data: str, design: QRDesign) -> str:
    """The QR code of `data` (a short link), drawn with `design`, as a standalone SVG document."""
    plan = layout(data, design)
    margin = design.margin
    side = plan.size + 2 * margin
    pixels = side * PIXELS_PER_MODULE
    rendering = (
        ' shape-rendering="crispEdges"' if design.module_style == "square" and design.eye_style == "square" else ""
    )
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {side} {side}" width="{pixels}" height="{pixels}"{rendering}>'
    ]
    if design.background != "transparent":
        parts.append(f'<rect width="{side}" height="{side}" fill="{design.background}"/>')
    parts.append(f'<g transform="translate({margin} {margin})">')
    parts.append(f'<g fill="{design.color}">{_modules(plan.modules, design.module_style)}</g>')
    eyes = "".join(_eye(x, y, design.eye_style) for x, y in plan.eyes)
    parts.append(f'<path fill="{design.eye_color}" fill-rule="evenodd" d="{eyes}"/>')
    if design.logo and plan.logo_area:
        x, y, area = plan.logo_area
        # The logo sits inside the cleared square, one module in from its edge
        inset = 1 if area > 4 else 0
        parts.append(
            f'<image href="{design.logo}" x="{x + inset}" y="{y + inset}" width="{area - 2 * inset}"'
            f' height="{area - 2 * inset}" preserveAspectRatio="xMidYMid meet"/>'
        )
    parts.append("</g></svg>")
    return "".join(parts)
