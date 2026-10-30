#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monta as animações de TODAS as emoções da Alicia a partir das folhas de quadros feitas no ChatGPT.

FERRAMENTA DE DESENVOLVIMENTO (não é usada na execução normal da Alicia). Precisa de:
    pip install numpy scipy Pillow

Entrada: uma folha por emoção (fundo verde #00FF00, 4 colunas x 5 linhas = 20 quadros):
    linhas 1-3 = 12 quadros de respiração (loop) | linha 4 = piscar (4) | linha 5 = boca falando (4)
    arquivos: serio.png  feliz.png  brava.png (ou bravo.png)  pensando.png  surpresa.png

Saída (por emoção):  assets/anim/<emoção>/idle_NN.png  blink_N.png  talk_N.png  anim.json

O que o script faz:
  1. recorta cada quadro e tira o fundo verde (chroma key, sem halo verde);
  2. alinha todos os quadros ao mesmo ponto (a grade do ChatGPT desloca cada coluna alguns pixels);
  3. nas linhas 4 e 5, que vêm cortadas na altura do pescoço, usa só o rosto e completa o corpo com o quadro de respiração;
  4. escala e posiciona na mesma moldura do avatar (500x461) e esmaece a base, como os demais PNGs;
  5. suaviza a respiração: quadros intermediários + um leve sobe-e-desce.
"""

import argparse
import json
import pathlib
import sys

import numpy as np
from PIL import Image, ImageFilter
from scipy import ndimage as ndi
from scipy.signal import fftconvolve

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from gerar_avatar import blend, key_green  # noqa: E402

# ---------------------------------------------------------------- geometria das folhas (1536x1024, 4x5)
COLS, COL_W, COL_MARGIN = 4, 384, 4
ROWS = [(2, 228), (231, 457), (460, 682), (686, 854), (857, 1022)]   # (topo, base) de cada linha de quadros
CELL_W = COL_W - 2 * COL_MARGIN                                       # 376
CELL_H = 226                                                          # altura da maior célula (linhas 1-2)
N_IDLE, N_BLINK, N_TALK = 12, 4, 4
GREEN = (0, 255, 0)

# ---------------------------------------------------------------- moldura final (a mesma dos PNGs do avatar)
OUT_W, OUT_H = 500, 461
SCALE = 1.78                  # escala célula -> tela (casa o rosto novo com o rosto do avatar anterior)
PASTE = (-69, 40)             # onde o canto da célula escalada cai na moldura
CUT, FADE = 216, 28           # linha de corte comum (em px de célula) e tamanho do esmaecimento da base
# o rosto vem dos quadros de piscar/falar; o resto vem da respiração: máscara vertical (px de célula)
FACE_UP0, FACE_UP1, FACE_DN0, FACE_DN1 = 44, 60, 130, 152
HEAD_BOX = (60, 10, 300, 125)  # região do rosto usada no alinhamento
SEARCH = 26
# Nas folhas do ChatGPT o quadro 11 da respiração veio com os olhos FECHADOS (um piscar no meio do loop).
# Ele sai do loop (vira a mistura dos vizinhos) e, se a linha de piscar não tiver olhos fechados, serve de "olho fechado".
IDLE_BLINK_FRAME = 10         # índice (0-based) do quadro de respiração com olhos fechados; -1 desliga

SHEET_NAMES = {"serio": ("serio",), "feliz": ("feliz",), "bravo": ("bravo", "brava"),
               "pensando": ("pensando",), "surpresa": ("surpresa",)}


# ---------------------------------------------------------------- utilidades de imagem
def hp(im):
    """Cinza com passa-alta (só os traços), olhando apenas o que não é fundo."""
    a = np.array(im).astype(np.float32)
    g = np.where(a[..., 3] > 128, a[..., :3].mean(-1), 128.0)
    return g - ndi.gaussian_filter(g, 5)


def ncc_shift(frame_hp, ref_hp, box=HEAD_BOX, rng=SEARCH):
    """Quanto mover o quadro (dx, dy, com sub-pixel) para casar com a referência. Devolve (dx, dy, qualidade)."""
    x0, y0, x1, y1 = box
    t = frame_hp[y0:y1, x0:x1]
    h, w = ref_hp.shape
    ry0, ry1, rx0, rx1 = max(0, y0 - rng), min(h, y1 + rng), max(0, x0 - rng), min(w, x1 + rng)
    r = ref_hp[ry0:ry1, rx0:rx1]
    num = fftconvolve(r, t[::-1, ::-1], mode="valid")
    en = fftconvolve(r ** 2, np.ones_like(t), mode="valid")
    sc = num / (np.sqrt(en) * np.linalg.norm(t) + 1e-6)
    iy, ix = np.unravel_index(np.argmax(sc), sc.shape)

    def parab(a, b, c):
        d = a - 2 * b + c
        return 0.0 if abs(d) < 1e-9 else 0.5 * (a - c) / d

    fx = parab(sc[iy, ix - 1], sc[iy, ix], sc[iy, ix + 1]) if 0 < ix < sc.shape[1] - 1 else 0.0
    fy = parab(sc[iy - 1, ix], sc[iy, ix], sc[iy + 1, ix]) if 0 < iy < sc.shape[0] - 1 else 0.0
    return float(rx0 + ix + fx - x0), float(ry0 + iy + fy - y0), float(sc[iy, ix])


def to_canvas(crop):
    """Põe o recorte (altura variável) numa célula de tamanho fixo, fundo verde."""
    c = Image.new("RGB", (CELL_W, CELL_H), GREEN)
    c.paste(crop, (0, 0))
    return c


def shift_rgb(img, dx, dy):
    """Move o conteúdo (+dx direita, +dy baixo) com sub-pixel; o que entra vem verde (vira transparente no key)."""
    return img.transform(img.size, Image.AFFINE, (1, 0, -dx, 0, 1, -dy), resample=Image.BICUBIC, fillcolor=GREEN)


def iris_amount(im):
    """Quanto de íris (lilás) aparece na região dos olhos: cai muito quando o olho fecha."""
    a = np.array(im).astype(int)
    lil = (a[..., 2] - a[..., 1] > 25) & (a[..., 2] > 120) & (a[..., 3] > 128)
    return int(lil[70:125, 85:290].sum())


def face_mask():
    y = np.arange(CELL_H, dtype=np.float32)
    up = np.clip((y - FACE_UP0) / (FACE_UP1 - FACE_UP0), 0, 1)
    dn = np.clip((FACE_DN1 - y) / (FACE_DN1 - FACE_DN0), 0, 1)
    return (up * dn)[:, None]


def head_composite(base, ov):
    """Rosto do quadro `ov` por cima do corpo de `base` (mistura com alfa pré-multiplicado, sem halos)."""
    b, o = np.array(base).astype(np.float32), np.array(ov).astype(np.float32)
    m = face_mask()
    pb, po = b[..., :3] * b[..., 3:] / 255, o[..., :3] * o[..., 3:] / 255
    alpha = b[..., 3] * (1 - m) + o[..., 3] * m
    prem = pb * (1 - m)[..., None] + po * m[..., None]
    rgb = np.where(alpha[..., None] > 0, prem * 255 / np.maximum(alpha[..., None], 1e-3), 0)
    return Image.fromarray(np.dstack([np.clip(rgb, 0, 255), alpha]).astype(np.uint8), "RGBA")


def cut_and_fade(im):
    """Corta na linha comum e esmaece a base (o corpo some suavemente, igual aos outros PNGs do avatar)."""
    a = np.array(im).copy()
    y = np.arange(a.shape[0], dtype=np.float32)
    ramp = np.clip((CUT - y) / FADE, 0, 1)
    a[..., 3] = (a[..., 3] * ramp[:, None]).astype(np.uint8)
    return Image.fromarray(a, "RGBA")


def to_output(cell):
    """Célula -> moldura 500x461 (escala, posição, leve nitidez)."""
    w, h = round(cell.width * SCALE), round(cell.height * SCALE)
    big = cell.resize((w, h), Image.LANCZOS)
    rgb = big.convert("RGB").filter(ImageFilter.UnsharpMask(radius=1.4, percent=55, threshold=2))
    big = Image.merge("RGBA", (*rgb.split(), big.getchannel("A")))
    canvas = Image.new("RGBA", (OUT_W, OUT_H), (0, 0, 0, 0))
    ox, oy = PASTE
    canvas.alpha_composite(big, (max(ox, 0), max(oy, 0)), (max(-ox, 0), max(-oy, 0)))
    return canvas


def breathe(canvas, phase, lift=3.0, grow=0.004):
    """Sobe e cresce um pouco (base fixa): soma ao movimento dos quadros. phase em 0..1 (loop perfeito)."""
    k = 0.5 - 0.5 * np.cos(2 * np.pi * phase)
    s = 1 + grow * k
    cx, cy = OUT_W / 2, round(CUT * SCALE + PASTE[1])
    a = 1 / s
    return canvas.transform(canvas.size, Image.AFFINE, (a, 0, cx - a * cx, 0, a, cy - a * cy + lift * k), resample=Image.BICUBIC)


# ---------------------------------------------------------------- uma folha
def find_sheet(folder, expr):
    for name in SHEET_NAMES[expr]:
        for ext in (".png", ".PNG", ".webp", ".jpg"):
            p = folder / f"{name}{ext}"
            if p.exists():
                return p
    raise FileNotFoundError(f"folha da emoção '{expr}' não encontrada em {folder} (procurei {SHEET_NAMES[expr]})")


def crops(sheet_path):
    im = Image.open(sheet_path).convert("RGB")
    if im.size != (1536, 1024):
        print(f"  AVISO: {sheet_path.name} tem {im.size}, o esperado é 1536x1024; confira os recortes.")
    out = []
    for (y0, y1) in ROWS:
        for c in range(COLS):
            out.append(to_canvas(im.crop((c * COL_W + COL_MARGIN, y0, (c + 1) * COL_W - COL_MARGIN, y1))))
    return out


def process_sheet(path, ref_hp, log):
    raw = crops(path)
    keyed = [key_green(c) for c in raw]
    # 1) respiração: cada quadro vai ao ponto de referência (tira o "tremido" da grade)
    idle, ref0 = [], None
    for i in range(N_IDLE):
        dx, dy, q = ncc_shift(hp(keyed[i]), ref_hp)
        idle.append(key_green(shift_rgb(raw[i], dx, dy)))
        if i == 0:
            ref0 = hp(idle[0])
            log.append(f"respiração: quadro 1 alinhado ({dx:+.1f},{dy:+.1f}) qualidade {q:.2f}")
    closed = None
    if 0 < IDLE_BLINK_FRAME < N_IDLE - 1:
        closed = idle[IDLE_BLINK_FRAME]
        idle[IDLE_BLINK_FRAME] = blend(idle[IDLE_BLINK_FRAME - 1], idle[IDLE_BLINK_FRAME + 1], 0.5)
        log.append(f"quadro {IDLE_BLINK_FRAME + 1} (olhos fechados) trocado pela média dos vizinhos")
    # 2) piscar e boca: alinhados ao quadro 1 da respiração; só o rosto entra, o corpo vem da respiração
    parts = {"blink": [], "talk": []}
    low = 1.0
    for name, start, n in (("blink", N_IDLE, N_BLINK), ("talk", N_IDLE + N_BLINK, N_TALK)):
        for i in range(n):
            dx, dy, q = ncc_shift(hp(keyed[start + i]), ref0)
            low = min(low, q)
            parts[name].append(head_composite(idle[0], key_green(shift_rgb(raw[start + i], dx, dy))))
    if closed is not None:                      # a linha de piscar tem de ter um quadro de olhos realmente fechados
        normal = float(np.median([iris_amount(f) for i, f in enumerate(idle) if i not in (IDLE_BLINK_FRAME,)]))
        if iris_amount(parts["blink"][2]) > 0.7 * normal:
            parts["blink"][2] = head_composite(idle[0], closed)
            log.append("piscar 3 refeito com o quadro de olhos fechados")
    log.append(f"piscar/boca: pior qualidade de alinhamento {low:.2f}")
    return idle, parts["blink"], parts["talk"], low


def build_emotion(expr, folder, out_root, inter, ref_hp, preview_dir):
    log = []
    path = find_sheet(folder, expr)
    idle, blink, talk, low = process_sheet(path, ref_hp, log)
    out = out_root / expr
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.png"):
        old.unlink()

    keys = [to_output(cut_and_fade(f)) for f in idle]
    loop = []
    for k in range(len(keys)):
        a, b = keys[k], keys[(k + 1) % len(keys)]
        for j in range(inter):
            loop.append(blend(a, b, j / inter) if j else a)
    idle_out = [breathe(c, i / len(loop)) for i, c in enumerate(loop)]
    blink_out = [to_output(cut_and_fade(f)) for f in blink]
    talk_out = [to_output(cut_and_fade(f)) for f in talk]
    for i, im in enumerate(idle_out, 1):
        im.save(out / f"idle_{i:02d}.png", optimize=True)
    for i, im in enumerate(blink_out, 1):
        im.save(out / f"blink_{i}.png", optimize=True)
    for i, im in enumerate(talk_out, 1):
        im.save(out / f"talk_{i}.png", optimize=True)
    manifest = {"expression": expr, "size": [OUT_W, OUT_H],
                "idle": {"frames": len(idle_out), "fps": 12},
                "blink": {"frames": len(blink_out), "sequence": [[2, 0.05], [3, 0.08], [4, 0.06]]},
                "talk": {"frames": len(talk_out), "fps": 8}}
    (out / "anim.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[{expr}] {path.name}: {len(idle_out)} respiração + {len(blink_out)} piscar + {len(talk_out)} boca | " + " | ".join(log))
    if low < 0.45:
        print(f"  AVISO: alinhamento fraco em {expr} ({low:.2f}); confira as prévias.")
    if preview_dir:
        make_previews(expr, idle_out, blink_out, talk_out, preview_dir)
    return manifest


def make_previews(expr, idle, blink, talk, folder):
    folder.mkdir(parents=True, exist_ok=True)
    bg = (38, 34, 46, 255)

    def comp(im):
        base = Image.new("RGBA", im.size, bg)
        base.alpha_composite(im)
        return base.convert("RGB").resize((round(im.width * 0.6), round(im.height * 0.6)), Image.LANCZOS)

    tl, dur = [], []
    for f in idle:
        tl.append(f); dur.append(83)
    for f, ms in zip((idle[0], blink[1], blink[2], blink[3], idle[0]), (400, 50, 80, 60, 400)):
        tl.append(f); dur.append(ms)
    for i in (0, 2, 1, 3, 2, 0, 3, 1, 2, 1, 0, 3):
        tl.append(talk[i]); dur.append(125)
    frames = [comp(f) for f in tl]
    frames[0].save(folder / f"{expr}.gif", save_all=True, append_images=frames[1:], duration=dur, loop=0, optimize=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--folhas", required=True, help="pasta com as folhas (serio.png, feliz.png, brava.png, pensando.png, surpresa.png)")
    ap.add_argument("--saida", default="assets/anim", help="pasta de saída (padrão: assets/anim)")
    ap.add_argument("--interpolar", type=int, default=2, help="quadros por passo da respiração (2 = 24 quadros de respiração)")
    ap.add_argument("--previas", help="pasta para salvar um GIF de prévia por emoção")
    args = ap.parse_args()
    folder, out_root = pathlib.Path(args.folhas), pathlib.Path(args.saida)
    preview = pathlib.Path(args.previas) if args.previas else None

    # referência única: o 1º quadro da folha "serio". Todas as emoções são alinhadas a ele.
    ref_raw = crops(find_sheet(folder, "serio"))[0]
    ref_hp = hp(key_green(ref_raw))
    for expr in SHEET_NAMES:
        build_emotion(expr, folder, out_root, max(1, args.interpolar), ref_hp, preview)


if __name__ == "__main__":
    main()
