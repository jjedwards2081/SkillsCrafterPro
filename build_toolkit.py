"""
Minecraft Build Toolkit — generates precise /fill and /setblock commands
from structured building plans.

The LLM outputs a JSON spec, and this module converts it to valid
Minecraft Bedrock commands using relative (~) coordinates.
"""

import json
import math


# ─── Valid Bedrock block names ───

MATERIALS = {
    # Wood types
    "oak_planks": "planks 0", "spruce_planks": "planks 1",
    "birch_planks": "planks 2", "jungle_planks": "planks 3",
    "acacia_planks": "planks 4", "dark_oak_planks": "planks 5",
    "oak_log": "log 0", "spruce_log": "log 1",
    "birch_log": "log 2", "jungle_log": "log 3",
    # Stone types
    "stone": "stone 0", "cobblestone": "cobblestone",
    "stone_bricks": "stonebrick 0", "mossy_stone_bricks": "stonebrick 1",
    "smooth_stone": "stone 7", "granite": "stone 1", "diorite": "stone 3",
    "andesite": "stone 5", "bricks": "brick_block",
    "sandstone": "sandstone 0", "red_sandstone": "red_sandstone 0",
    # Glass
    "glass": "glass", "glass_pane": "glass_pane",
    # Stairs
    "oak_stairs": "oak_stairs", "spruce_stairs": "spruce_stairs",
    "birch_stairs": "birch_stairs", "stone_stairs": "stone_stairs",
    "cobblestone_stairs": "stone_stairs", "brick_stairs": "brick_stairs",
    "sandstone_stairs": "sandstone_stairs",
    # Slabs
    "oak_slab": "wooden_slab 0", "stone_slab": "stone_slab 0",
    "cobblestone_slab": "stone_slab 3", "brick_slab": "stone_slab 4",
    # Doors
    "oak_door": "wooden_door", "spruce_door": "spruce_door",
    "birch_door": "birch_door", "iron_door": "iron_door",
    # Other
    "wool": "wool 0", "white_wool": "wool 0", "red_wool": "wool 14",
    "blue_wool": "wool 11", "green_wool": "wool 13",
    "torch": "torch", "lantern": "lantern",
    "crafting_table": "crafting_table", "furnace": "furnace",
    "chest": "chest", "bookshelf": "bookshelf",
    "ladder": "ladder", "fence": "fence 0", "oak_fence": "fence 0",
    "iron_bars": "iron_bars", "carpet": "carpet 0",
    "grass": "grass", "dirt": "dirt", "sand": "sand",
    "water": "water", "lava": "lava",
    "glowstone": "glowstone", "sea_lantern": "sea_lantern",
    "quartz_block": "quartz_block 0", "concrete": "concrete 0",
    "white_concrete": "concrete 0", "black_concrete": "concrete 15",
}


def _block(name):
    """Resolve a block name to a Bedrock block ID string."""
    return MATERIALS.get(name, name)


def _fill(x1, y1, z1, x2, y2, z2, block, hollow=False):
    """Generate a /fill command with relative coordinates."""
    mode = " hollow" if hollow else ""
    return f"fill ~{x1} ~{y1} ~{z1} ~{x2} ~{y2} ~{z2} {_block(block)}{mode}"


def _setblock(x, y, z, block):
    """Generate a /setblock command with relative coordinates."""
    return f"setblock ~{x} ~{y} ~{z} {_block(block)}"


# ─── Building Primitives ───

def build_box(w, d, h, material, hollow=True):
    """Solid or hollow box. Origin at player's feet, extends +X and +Z."""
    cmds = []
    if hollow:
        cmds.append(_fill(0, 0, 0, w-1, h-1, d-1, material, hollow=True))
    else:
        cmds.append(_fill(0, 0, 0, w-1, h-1, d-1, material))
    return cmds


def build_floor(w, d, material, y=0):
    """Flat floor at height y."""
    return [_fill(0, y, 0, w-1, y, d-1, material)]


def build_walls(w, d, h, material, y=0):
    """Four walls (no floor, no ceiling)."""
    cmds = []
    # Front wall (z=0)
    cmds.append(_fill(0, y, 0, w-1, y+h-1, 0, material))
    # Back wall (z=d-1)
    cmds.append(_fill(0, y, d-1, w-1, y+h-1, d-1, material))
    # Left wall (x=0)
    cmds.append(_fill(0, y, 0, 0, y+h-1, d-1, material))
    # Right wall (x=w-1)
    cmds.append(_fill(w-1, y, 0, w-1, y+h-1, d-1, material))
    return cmds


def build_roof_flat(w, d, material, y):
    """Flat roof/ceiling."""
    return [_fill(0, y, 0, w-1, y, d-1, material)]


def build_roof_peaked(w, d, material, y):
    """Simple peaked roof using stairs and slabs along X axis."""
    cmds = []
    # Determine stair material
    stair = material
    if "planks" in material:
        wood = material.split("_")[0]
        stair = f"{wood}_stairs"
    elif material in ("cobblestone", "stone", "stone_bricks"):
        stair = "stone_stairs"
    elif material == "bricks":
        stair = "brick_stairs"
    elif material == "sandstone":
        stair = "sandstone_stairs"

    half_w = w // 2
    for layer in range(half_w + 1):
        left_x = layer
        right_x = w - 1 - layer
        ry = y + layer

        if left_x >= right_x:
            # Peak — use slabs or full block
            cmds.append(_fill(left_x, ry, -1, right_x, ry, d, material))
            break

        # Left slope
        cmds.append(_fill(left_x, ry, -1, left_x, ry, d, stair))
        # Right slope (facing other way — use the block, stairs facing is tricky)
        cmds.append(_fill(right_x, ry, -1, right_x, ry, d, stair))
        # Fill between with the material
        if right_x - left_x > 1:
            cmds.append(_fill(left_x+1, ry, -1, right_x-1, ry, d, material))

    return cmds


def build_door(x, z, facing="z"):
    """Place a door at ground level."""
    return [
        _fill(x, 1, z, x, 2, z, "air"),
        _setblock(x, 1, z, "oak_door"),
    ]


def build_window(x, y, z):
    """Place a 1x1 glass pane."""
    return [_setblock(x, y, z, "glass_pane")]


def build_windows_wall(start_x, start_z, length, y, axis="x", spacing=3):
    """Place windows along a wall at regular intervals."""
    cmds = []
    for i in range(1, length - 1, spacing):
        if axis == "x":
            cmds.append(_setblock(start_x + i, y, start_z, "glass_pane"))
        else:
            cmds.append(_setblock(start_x, y, start_z + i, "glass_pane"))
    return cmds


def build_interior(w, d, items=None):
    """Place interior items like torches, crafting table, etc."""
    cmds = []
    if items is None:
        items = ["torch", "crafting_table"]

    if "torch" in items:
        cmds.append(_setblock(1, 3, 1, "torch"))
        cmds.append(_setblock(w-2, 3, 1, "torch"))
        cmds.append(_setblock(1, 3, d-2, "torch"))
        cmds.append(_setblock(w-2, 3, d-2, "torch"))

    if "crafting_table" in items:
        cmds.append(_setblock(1, 1, 1, "crafting_table"))

    if "furnace" in items:
        cmds.append(_setblock(2, 1, 1, "furnace"))

    if "chest" in items:
        cmds.append(_setblock(1, 1, d-2, "chest"))

    if "bed" in items:
        cmds.append(_setblock(w-2, 1, d-2, "wool"))
        cmds.append(_setblock(w-2, 1, d-3, "wool"))

    return cmds


# ─── Compound Structures ───

def build_house(spec):
    """Build a complete house from a spec dict."""
    w = spec.get("width", 7)
    d = spec.get("depth", 5)
    h = spec.get("height", 4)
    material = spec.get("material", "oak_planks")
    floor_mat = spec.get("floor", material)
    roof_type = spec.get("roof_type", "peaked")
    roof_mat = spec.get("roof_material", material)
    has_door = spec.get("door", True)
    num_windows = spec.get("windows", 2)
    interior = spec.get("interior", ["torch", "crafting_table"])

    cmds = []
    # Clear space
    cmds.append(_fill(0, 0, 0, w-1, h+3, d-1, "air"))
    # Floor
    cmds.extend(build_floor(w, d, floor_mat))
    # Walls
    cmds.extend(build_walls(w, d, h, material, y=1))
    # Clear interior
    cmds.append(_fill(1, 1, 1, w-2, h-1, d-2, "air"))
    # Roof
    if roof_type == "peaked":
        cmds.extend(build_roof_peaked(w, d, roof_mat, y=h+1))
    else:
        cmds.extend(build_roof_flat(w, d, roof_mat, y=h))
    # Door (front wall, center)
    if has_door:
        door_x = w // 2
        cmds.extend(build_door(door_x, 0))
    # Windows
    if num_windows > 0:
        cmds.extend(build_windows_wall(0, 0, w, y=2, axis="x", spacing=max(2, w // (num_windows + 1))))
        cmds.extend(build_windows_wall(0, d-1, w, y=2, axis="x", spacing=max(2, w // (num_windows + 1))))
    # Interior
    cmds.extend(build_interior(w, d, interior))

    return cmds


def build_tower(spec):
    """Build a tower."""
    radius = spec.get("radius", 3)
    h = spec.get("height", 10)
    material = spec.get("material", "cobblestone")
    w = radius * 2 + 1

    cmds = []
    cmds.append(_fill(0, 0, 0, w-1, h, w-1, "air"))
    cmds.extend(build_walls(w, w, h, material, y=0))
    cmds.append(_fill(1, 0, 1, w-2, 0, w-2, material))  # floor
    cmds.append(_fill(1, 1, 1, w-2, h-1, w-2, "air"))  # interior
    cmds.extend(build_roof_flat(w, w, material, y=h))
    # Battlements
    for i in range(0, w, 2):
        cmds.append(_setblock(i, h+1, 0, material))
        cmds.append(_setblock(i, h+1, w-1, material))
        cmds.append(_setblock(0, h+1, i, material))
        cmds.append(_setblock(w-1, h+1, i, material))
    cmds.extend(build_door(w // 2, 0))
    cmds.append(_setblock(1, 2, 1, "torch"))
    cmds.append(_setblock(w-2, 2, w-2, "torch"))
    return cmds


def build_bridge(spec):
    """Build a bridge."""
    length = spec.get("length", 15)
    width = spec.get("width", 3)
    material = spec.get("material", "oak_planks")
    railing = spec.get("railing", "oak_fence")

    cmds = []
    # Deck
    cmds.append(_fill(0, 0, 0, width-1, 0, length-1, material))
    # Railings
    cmds.append(_fill(0, 1, 0, 0, 1, length-1, railing))
    cmds.append(_fill(width-1, 1, 0, width-1, 1, length-1, railing))
    # Torches at intervals
    for i in range(0, length, 4):
        cmds.append(_setblock(0, 2, i, "torch"))
        cmds.append(_setblock(width-1, 2, i, "torch"))
    return cmds


def build_wall_structure(spec):
    """Build a defensive wall."""
    length = spec.get("length", 20)
    h = spec.get("height", 5)
    material = spec.get("material", "cobblestone")

    cmds = []
    cmds.append(_fill(0, 0, 0, 1, h, length-1, material))
    # Battlements
    for i in range(0, length, 2):
        cmds.append(_setblock(0, h+1, i, material))
        cmds.append(_setblock(1, h+1, i, material))
    # Walkway
    cmds.append(_fill(0, h-1, 0, 1, h-1, length-1, material))
    return cmds


def build_pool(spec):
    """Build a pool/pond."""
    w = spec.get("width", 6)
    d = spec.get("depth", 6)
    pool_depth = spec.get("pool_depth", 2)
    material = spec.get("material", "stone_bricks")

    cmds = []
    # Dig
    cmds.append(_fill(0, -pool_depth, 0, w-1, 0, d-1, "air"))
    # Walls and floor
    cmds.append(_fill(0, -pool_depth, 0, w-1, 0, d-1, material, hollow=True))
    # Water
    cmds.append(_fill(1, -pool_depth+1, 1, w-2, -1, d-2, "water"))
    return cmds


def build_farm(spec):
    """Build a small farm."""
    w = spec.get("width", 9)
    d = spec.get("depth", 9)

    cmds = []
    # Clear and flatten
    cmds.append(_fill(0, 0, 0, w-1, 0, d-1, "dirt"))
    cmds.append(_fill(0, 1, 0, w-1, 3, d-1, "air"))
    # Fence
    cmds.append(_fill(0, 1, 0, w-1, 1, 0, "oak_fence"))
    cmds.append(_fill(0, 1, d-1, w-1, 1, d-1, "oak_fence"))
    cmds.append(_fill(0, 1, 0, 0, 1, d-1, "oak_fence"))
    cmds.append(_fill(w-1, 1, 0, w-1, 1, d-1, "oak_fence"))
    # Water channel in middle
    mid = w // 2
    cmds.append(_fill(mid, 0, 1, mid, 0, d-2, "water"))
    # Torches at corners
    cmds.append(_setblock(1, 2, 1, "torch"))
    cmds.append(_setblock(w-2, 2, 1, "torch"))
    cmds.append(_setblock(1, 2, d-2, "torch"))
    cmds.append(_setblock(w-2, 2, d-2, "torch"))
    # Gate
    cmds.append(_fill(mid, 1, 0, mid, 1, 0, "air"))
    return cmds


def build_fountain(spec):
    """Build a fountain."""
    material = spec.get("material", "stone_bricks")

    cmds = []
    # Base pool
    cmds.append(_fill(-2, 0, -2, 2, 0, 2, material))
    cmds.append(_fill(-1, 0, -1, 1, 0, 1, "water"))
    # Center pillar
    cmds.append(_setblock(0, 1, 0, material))
    cmds.append(_setblock(0, 2, 0, material))
    cmds.append(_setblock(0, 3, 0, "water"))
    # Rim decoration
    for x, z in [(-2,-2),(2,-2),(-2,2),(2,2)]:
        cmds.append(_setblock(x, 1, z, material))
        cmds.append(_setblock(x, 2, z, "torch"))
    return cmds


# ─── Structure Registry ───

STRUCTURE_BUILDERS = {
    "house": build_house,
    "tower": build_tower,
    "bridge": build_bridge,
    "wall": build_wall_structure,
    "pool": build_pool,
    "pond": build_pool,
    "farm": build_farm,
    "fountain": build_fountain,
}

# Template defaults for the LLM to reference
STRUCTURE_SPECS = {
    "house": {
        "type": "house", "width": 7, "depth": 5, "height": 4,
        "material": "oak_planks", "floor": "oak_planks",
        "roof_type": "peaked", "roof_material": "oak_planks",
        "door": True, "windows": 2,
        "interior": ["torch", "crafting_table", "furnace", "chest"],
    },
    "tower": {
        "type": "tower", "radius": 3, "height": 10,
        "material": "cobblestone",
    },
    "bridge": {
        "type": "bridge", "length": 15, "width": 3,
        "material": "oak_planks", "railing": "oak_fence",
    },
    "wall": {
        "type": "wall", "length": 20, "height": 5,
        "material": "cobblestone",
    },
    "pool": {
        "type": "pool", "width": 6, "depth": 6, "pool_depth": 2,
        "material": "stone_bricks",
    },
    "farm": {
        "type": "farm", "width": 9, "depth": 9,
    },
    "fountain": {
        "type": "fountain", "material": "stone_bricks",
    },
}


def generate_build_commands(spec):
    """Given a structure spec dict, generate Minecraft commands.
    Returns a list of command strings (without leading /).
    """
    struct_type = spec.get("type", "house")
    builder = STRUCTURE_BUILDERS.get(struct_type)
    if not builder:
        return [f"say Unknown structure type: {struct_type}"]
    return builder(spec)


def get_llm_build_prompt():
    """Return the system prompt for the LLM build planner."""
    types_desc = ", ".join(STRUCTURE_SPECS.keys())
    examples = json.dumps({"type": "house", "width": 9, "depth": 7, "height": 5,
                           "material": "stone_bricks", "roof_type": "peaked",
                           "roof_material": "oak_planks", "door": True, "windows": 3,
                           "interior": ["torch", "crafting_table", "furnace", "chest"]})
    return (
        f"You are a Minecraft build planner. The player describes what they want built. "
        f"You output a JSON spec that a build system will use to construct it.\n\n"
        f"Available structure types: {types_desc}\n\n"
        f"Available materials: {', '.join(sorted(MATERIALS.keys()))}\n\n"
        f"Spec fields per type:\n"
        + "\n".join(f"  {k}: {json.dumps(v)}" for k, v in STRUCTURE_SPECS.items())
        + f"\n\nReturn ONLY a JSON object like: {examples}\n"
        f"Choose appropriate dimensions and materials based on what the player asks for. "
        f"If they say 'big house', increase width/depth. If they say 'stone tower', use cobblestone. "
        f"If the request doesn't match any type, pick the closest one."
    )
