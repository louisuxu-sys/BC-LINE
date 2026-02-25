import sys
sys.path.insert(0, '.')
from sv94 import compute_big_road_cols, _derived_road, _derived_to_cols

# Input: 1132122221221122232311122132212213
# 1=閒(P), 2=莊(B), 3=和(T)
raw = '1132122221221122232311122132212213'
code_map = {'1': '閒', '2': '莊', '3': '和'}
history = [code_map[c] for c in raw if c in code_map]

pure = [h for h in history if h in ('莊', '閒')]
print("Pure:", ''.join('B' if h == '莊' else 'P' for h in pure))

cols = compute_big_road_cols(history)
print("\nBig road cols:")
for i, col in enumerate(cols):
    labels = ['B' if h == '莊' else 'P' for h in col]
    print(f"  col{i}: {''.join(labels)} (len={len(col)})")

# Big Eye Boy (gap=1)
big_eye = _derived_road(cols, 1)
print(f"\nBig Eye Boy ({len(big_eye)} items): {''.join(big_eye)}")
big_eye_cols = _derived_to_cols(big_eye)
print("Big Eye cols:")
for i, col in enumerate(big_eye_cols):
    print(f"  col{i}: {''.join(col)} (len={len(col)})")

# Small Road (gap=2)
small_r = _derived_road(cols, 2)
print(f"\nSmall Road ({len(small_r)} items): {''.join(small_r)}")
small_cols = _derived_to_cols(small_r)
print("Small Road cols:")
for i, col in enumerate(small_cols):
    print(f"  col{i}: {''.join(col)} (len={len(col)})")

# Cockroach (gap=3)
cockroach = _derived_road(cols, 3)
print(f"\nCockroach ({len(cockroach)} items): {''.join(cockroach)}")
cock_cols = _derived_to_cols(cockroach)
print("Cockroach cols:")
for i, col in enumerate(cock_cols):
    print(f"  col{i}: {''.join(col)} (len={len(col)})")
