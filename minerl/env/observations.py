from copy import deepcopy
import os

dir_path = os.path.dirname(os.path.realpath(__file__))


def get_item_block_lists():
    """
    :return: items: a list of strings, corresponding to minecraft items in a player's inventory.
            blocks: a list of strings, corresponding to minecraft blocks in the world.

        Where do items.txt and blocks.txt come from? Better for your sanity if you pretend they're magic.

        Malmo has two files: `Schemas/MinecraftItems.txt` and `Schemas/MinecraftBlocks.txt`.
        You will note the item list is not exhaustive - it doesn't have dirt, and that can be in an inventory.
        So, look at items.json. That seems like an exhaustive list of inventory items.
        However, it has more entries than MinecraftItems.txt and MinecraftBlocks.txt combined. i.e., both are wrong.
        So, we use items.json for our items list. For our block list, we use the `Blocks` section of `Schemas/Types.xsd`
        Why do we use that? It has a reasonable length. I have no means to confirm its accuracy.
    """

    if not (os.path.isdir(os.path.join(dir_path, 'data')) and
            os.path.isfile(os.path.join(dir_path, 'data', 'items.txt')) and
            os.path.isfile(os.path.join(dir_path, 'data', 'blocks.txt'))):
        print("Could not find item and block data!")
        return None
    item = []
    block = []
    with open(os.path.join(dir_path, 'data', 'items.txt')) as f:
        for line in f:
            item.append(line.strip())

    with open(os.path.join(dir_path, 'data', 'blocks.txt')) as f:
        for line in f:
            block.append(line.strip())
    return item, block


ITEMS, BLOCKS = get_item_block_lists()
# Use these for observation spaces that interact with minecraft items and blocks.
#   Provides a non-meaningful but consistent ordering over the space of names.
# Currently no support for entities.


def pov_observation(info, obs_space):
    """
    :param info: Info dict generated by Malmo mod. Must contain 'pov'.
    :param obs_space: unused.
    :return: np.array with shape specified in Mission xml file.
    """
    return info['pov']


def inventory_observation(info, obs_space):
    """
    For every item type in the observation space, add up how many of said item are in the inventory.
    Duplicates the MineRL default inventory observation.

    :param info: Info dict generated by Malmo mod.
    :param obs_space: The observation space. Used to decide which item information to include.
    :return: Dict of {item name : count in inventory}.
    """
    inventory_spaces = obs_space['inventory'].spaces

    inventory_dict = {k: 0 for k in inventory_spaces}
    # TODO change to maalmo
    if 'inventory' in info:
        for stack in info['inventory']:
            if 'type' in stack and 'quantity' in stack:
                type_name = stack['type']
                if type_name == 'log2':
                    type_name = 'log'

                try:
                    inventory_dict[type_name] += stack['quantity']
                except ValueError:
                    continue
                except KeyError:
                    # We only care to observe what was specified in the space.
                    continue
    else:
        print("Inventory information could not be found, returning empty inventory.")

    return inventory_dict
