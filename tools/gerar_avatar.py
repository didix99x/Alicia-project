#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gera as imagens do avatar (estáticas + animações) a partir das grades feitas no GPT.

FERRAMENTA DE DESENVOLVIMENTO (não é usada na execução normal da Alicia). Precisa de:
    pip install numpy scipy Pillow

Entradas (grades com fundo verde chapado, ver o README):
    --padrao    alicia_padrao.png (recorte base, expressão "serio")
    --folha     folha de 7 expressões (fundo escuro com molduras)  [opcional: regenera as estáticas]
    --respirar  grade 2x3 (6 quadros)    --falar  grade 2x2 (4 bocas)    --piscar  grade 2x2 (4 quadros)
Saída: assets/<expressão>.png (500 px de largura) e assets/anim/{idle,blink,talk}_N.png + anim.json
"""

import argparse
import json
import pathlib

import numpy as np
from PIL import Image, ImageFilter
from scipy import ndimage as ndi
from scipy.signal import fftconvolve

# Canvas de trabalho (cabeça com 347 px de largura, topo em y=36, centro em x=320)
W, H, TOP, CX, TARGET_HEAD = 640, 820, 36, 320, 347
CUT = 590              # linha de corte comum a TODAS as imagens (o corpo some suavemente até aqui)
FADE_PX = 70
OUT_W = 500            # largura final = o tamanho em que a janela exibe o avatar
BOX = (150, 110, 500, 330)   # região do rosto usada no alinhamento
HEAD_ROWS = 330


# ---------------------------------------------------------------- chroma key e recortes
def split_cells(path, cols, rows, margin=3):
    im = Image.open(path).convert("RGB")
    w, h = im.size
    cw, ch = w / cols, h / rows
    return [im.crop((round(c * cw) + margin, round(r * ch) + margin, round((c + 1) * cw) - margin, round((r + 1) * ch) - margin))
            for r in range(rows) for c in range(cols)]


def key_green(img, lo=25, hi=110):
    a = np.asarray(img).astype(np.float32)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    t = np.clip((g - np.maximum(r, b) - lo) / (hi - lo), 0, 1)
    alpha = 1 - t * t * (3 - 2 * t)
    out = np.stack([r, np.minimum(g, np.maximum(r, b) + 4), b], -1)          # tira o respingo verde
    mask = alpha > 0.5
    lab, n = ndi.label(mask)
    if n > 1:
        sizes = ndi.sum(mask, lab, range(1, n + 1))
        keep = np.isin(lab, [i + 1 for i, s in enumerate(sizes) if s > 0.003 * mask.size])
        alpha = alpha * ndi.binary_dilation(keep, iterations=2)
    alpha = np.minimum(alpha, ndi.uniform_filter(alpha, 3) * 1.15)
    return Image.fromarray(np.dstack([np.clip(out, 0, 255), np.clip(alpha, 0, 1) * 255]).astype(np.uint8), "RGBA")


def matte_sheet_panel(sheet_path, box):
    """Recorta um painel da folha (fundo escuro com degradê) e remove o fundo por diferença de cor."""
    arr = np.array(Image.open(sheet_path).convert("RGB").crop(box)).astype(float)
    h, w, _ = arr.shape

    def feats(a):
        return np.concatenate([a, (3.0 * (a[..., 0] - a[..., 2]))[..., None]], -1)

    yy, xx = np.mgrid[0:h, 0:w]
    X = np.stack([np.ones_like(xx), xx / w, yy / h, (xx / w) ** 2, (yy / h) ** 2, (xx * yy) / (w * h)], -1).reshape(-1, 6).astype(float)
    ring = np.zeros((h, w), bool)
    ring[:14, :] = ring[:, :14] = ring[:, -14:] = True
    F, model = feats(arr), np.zeros((h, w, 4))
    for c in range(4):
        y = F[..., c].reshape(-1)
        m = ring.reshape(-1).copy()
        for _ in range(5):
            coef, *_ = np.linalg.lstsq(X[m], y[m], rcond=None)
            m = ring.reshape(-1) & (np.abs(X @ coef - y) < 14)
        model[..., c] = (X @ coef).reshape(h, w)
    cand = np.sqrt(((F - model) ** 2).sum(-1)) < 30
    lab, _ = ndi.label(cand)
    edge = np.unique(np.concatenate([lab[0], lab[:, 0], lab[:, -1]]))
    bg = ndi.binary_opening(np.isin(lab, edge[edge > 0]), iterations=1)
    fg = ~bg
    lab, n = ndi.label(fg)
    if n > 1:
        sizes = ndi.sum(fg, lab, range(1, n + 1))
        fg = np.isin(lab, [i + 1 for i, s in enumerate(sizes) if s > 0.02 * h * w])
    fg = ndi.binary_erosion(ndi.binary_fill_holes(fg), iterations=1)
    alpha = Image.fromarray((fg * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.9))
    out = Image.fromarray(arr.astype(np.uint8)).convert("RGBA")
    out.putalpha(alpha)
    return out


# ---------------------------------------------------------------- geometria
def head_band(im):
    a = np.array(im)[..., 3] > 128
    ys, xs = np.where(a)
    y0, y1 = ys.min(), ys.max()
    cols = np.where(a[y0:y0 + int((y1 - y0) * 0.30)].any(0))[0]
    return y0, cols.min(), cols.max()


def place(im, s, ox, oy, ang=0.0):
    r = im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))), Image.LANCZOS)
    if ang:
        w0, h0 = r.size
        r = r.rotate(ang, resample=Image.BICUBIC, expand=True)
        ox -= (r.width - w0) // 2
        oy -= (r.height - h0) // 2
    c = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    c.alpha_composite(r, (max(ox, 0), max(oy, 0)), (max(-ox, 0), max(-oy, 0)))
    return c


def head_transform(im):
    y0, xa, xb = head_band(im)
    s = TARGET_HEAD / float(xb - xa)
    y0s, xas, xbs = head_band(im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS))
    return s, CX - (xas + xbs) // 2, TOP - y0s


def gray_hp(im):
    a = np.array(im).astype(np.float32)
    g = np.where(a[..., 3] > 128, a[..., :3].mean(-1), 128.0)
    return g - ndi.gaussian_filter(g, 6)


def face_align(first, ref, angles=(0,), srange=0.12, steps=25, margin=60):
    """Acha escala/rotação/deslocamento que casam o rosto do 1º quadro com a referência."""
    R = gray_hp(ref)[BOX[1]:BOX[3], BOX[0]:BOX[2]]
    s0, ox0, oy0 = head_transform(first)
    best = None
    for ang in angles:
        for ds in np.linspace(-srange, srange, steps):
            s = s0 * (1 + ds)
            A = gray_hp(place(first, s, ox0, oy0, ang))
            y0, y1, x0, x1 = BOX[1] - margin, BOX[3] + margin, BOX[0] - margin, BOX[2] + margin
            sub = A[max(y0, 0):min(y1, H), max(x0, 0):min(x1, W)]
            num = fftconvolve(sub, R[::-1, ::-1], mode="valid")
            en = fftconvolve(sub ** 2, np.ones_like(R), mode="valid")
            sc = num / (np.sqrt(en) * np.linalg.norm(R) + 1e-6)
            iy, ix = np.unravel_index(np.argmax(sc), sc.shape)
            dy, dx = (BOX[1] - max(y0, 0)) - iy, (BOX[0] - max(x0, 0)) - ix
            if best is None or sc[iy, ix] > best[0]:
                best = (float(sc[iy, ix]), s, ox0 + int(dx), oy0 + int(dy), ang)
    return best


def finish(canvas):
    """Corta na linha comum, esmaece a base e reduz para a largura de exibição."""
    arr = np.array(canvas)[:CUT].copy()
    ramp = np.clip((CUT - np.arange(CUT)) / FADE_PX, 0, 1)
    arr[..., 3] = (arr[..., 3] * ramp[:, None]).astype(np.uint8)
    img = Image.fromarray(arr, "RGBA")
    return img.resize((OUT_W, round(CUT * OUT_W / W)), Image.LANCZOS)


def breathe(canvas, phase, lift=4.0, grow=0.004):
    """Respiração procedural: sobe e cresce um pouco (centro-baixo fixo) — soma ao movimento dos quadros."""
    k = 0.5 - 0.5 * np.cos(2 * np.pi * phase)
    s = 1 + grow * k
    # transformação afim inversa (saída -> entrada), escala em torno do centro-baixo
    cx, cy = W / 2, CUT
    a = 1 / s
    c_off = cx - a * cx
    f_off = cy - a * cy + lift * k
    return canvas.transform(canvas.size, Image.AFFINE, (a, 0, c_off, 0, a, f_off), resample=Image.BICUBIC)


def blend(a, b, t):
    """Mistura de quadros com alfa pré-multiplicado (sem halos)."""
    fa, fb = np.array(a).astype(np.float32), np.array(b).astype(np.float32)
    pa, pb = fa[..., :3] * fa[..., 3:] / 255, fb[..., :3] * fb[..., 3:] / 255
    alpha = fa[..., 3] * (1 - t) + fb[..., 3] * t
    prem = pa * (1 - t) + pb * t
    rgb = np.where(alpha[..., None] > 0, prem * 255 / np.maximum(alpha[..., None], 1e-3), 0)
    return Image.fromarray(np.dstack([np.clip(rgb, 0, 255), alpha]).astype(np.uint8), "RGBA")


# ---------------------------------------------------------------- montagem
SHEET_BOXES = {1: (22, 18, 276, 413), 2: (294, 18, 554, 413), 3: (572, 18, 826, 413),
               4: (22, 434, 414, 831), 5: (434, 434, 826, 831), 6: (22, 853, 414, 1246), 7: (434, 853, 826, 1246)}
SHEET_MAP = {"feliz": 5, "bravo": 2, "pensando": 3, "surpresa": 4, "feliz_2": 6, "feliz_3": 7}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--padrao", required=True)
    ap.add_argument("--folha")
    ap.add_argument("--respirar", required=True)
    ap.add_argument("--falar", required=True)
    ap.add_argument("--piscar", required=True)
    ap.add_argument("--saida", default="assets")
    ap.add_argument("--previas", help="pasta para salvar GIFs de prévia")
    args = ap.parse_args()
    out = pathlib.Path(args.saida)
    (out / "anim").mkdir(parents=True, exist_ok=True)

    # referência: o recorte "padrão" colocado no canvas de trabalho
    pad = Image.open(args.padrao).convert("RGBA")
    s, ox, oy = head_transform(pad)
    ref = place(pad, s, ox, oy)
    finish(ref).save(out / "serio.png", optimize=True)

    if args.folha:
        for name, idx in SHEET_MAP.items():
            im = matte_sheet_panel(args.folha, tuple(v + (3 if i < 2 else -3) for i, v in enumerate(SHEET_BOXES[idx])))
            s, ox, oy = head_transform(im)
            finish(place(im, s, ox, oy)).save(out / f"{name}.png", optimize=True)

    sets = {}
    for name, (path, cols, rows, angles) in {
        "blink": (args.piscar, 2, 2, (0,)), "talk": (args.falar, 2, 2, (0,)),
        "idle": (args.respirar, 2, 3, tuple(np.arange(-6, 6.1, 1.0))),
    }.items():
        frames = [key_green(c) for c in split_cells(path, cols, rows)]
        score, s, ox, oy, ang = face_align(frames[0], ref, angles=angles)
        print(f"{name}: casamento do rosto={score:.2f} escala={s:.3f} ângulo={ang:.0f}")
        sets[name] = [place(f, s, ox, oy, ang) for f in frames]

    for i, c in enumerate(sets["blink"], 1):
        finish(c).save(out / "anim" / f"blink_{i}.png", optimize=True)
    for i, c in enumerate(sets["talk"], 1):
        finish(c).save(out / "anim" / f"talk_{i}.png", optimize=True)

    keys, inter, loop = sets["idle"], 3, []
    for k in range(len(keys)):
        a, b = keys[k], keys[(k + 1) % len(keys)]
        for j in range(inter):
            loop.append(blend(a, b, j / inter))
    idle = [finish(breathe(c, i / len(loop))) for i, c in enumerate(loop)]
    for i, im in enumerate(idle, 1):
        im.save(out / "anim" / f"idle_{i:02d}.png", optimize=True)

    manifest = {
        "expression": "serio", "size": list(idle[0].size),
        "idle": {"frames": len(idle), "fps": 10},
        "blink": {"frames": len(sets["blink"]), "sequence": [[2, 0.05], [3, 0.08], [4, 0.06]]},
        "talk": {"frames": len(sets["talk"]), "fps": 8},
    }
    (out / "anim" / "anim.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("manifesto:", manifest)

    if args.previas:
        pre = pathlib.Path(args.previas)
        pre.mkdir(parents=True, exist_ok=True)
        bg = (38, 34, 46, 255)

        def comp(im, scale=0.72):
            base = Image.new("RGBA", im.size, bg)
            base.alpha_composite(im)
            return base.convert("RGB").resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)

        def gif(name, frames, ms):
            frames = [comp(f) for f in frames]
            frames[0].save(pre / name, save_all=True, append_images=frames[1:], duration=ms, loop=0, optimize=True)

        gif("previa_respirar.gif", idle, 100)
        gif("previa_piscar.gif", [idle[0], finish(sets["blink"][1]), finish(sets["blink"][2]), finish(sets["blink"][3]), idle[0]] * 1, [900, 50, 80, 60, 900])
        talk = [finish(c) for c in sets["talk"]]
        order = [0, 2, 1, 3, 2, 0, 3, 1, 2, 1, 0, 3, 2, 1, 0]
        gif("previa_falar.gif", [talk[i] for i in order], 125)
        # linha do tempo realista: respira, pisca, fala, respira
        tl, dur = [], []
        for f in idle[:20]: tl.append(f); dur.append(100)
        for k, ms in ((1, 50), (2, 80), (3, 60)): tl.append(finish(sets["blink"][k])); dur.append(ms)
        for f in idle[:8]: tl.append(f); dur.append(100)
        for i in order * 2: tl.append(talk[i]); dur.append(125)
        for f in idle: tl.append(f); dur.append(100)
        gif("previa_completa.gif", tl, dur)
        print("prévias em", pre)


if __name__ == "__main__":
    main()
