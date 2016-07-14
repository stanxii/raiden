""" Utilities to set-up a Raiden network. """
from __future__ import print_function

import copy
import json
import os
import random
import shlex
import time
from math import floor
from subprocess import Popen, PIPE

from devp2p.crypto import privtopub
from devp2p.utils import host_port_pubkey_to_uri
from ethereum.keys import privtoaddr
from ethereum.utils import denoms, privtoaddr, encode_hex
from ethereum.slogging import getLogger
from pyethapp.accounts import Account
from pyethapp.config import update_config_from_genesis_json
from pyethapp.console_service import Console
from pyethapp.rpc_client import JSONRPCClient

from raiden.app import App, INITIAL_PORT
from raiden.network.discovery import Discovery
from raiden.network.rpc.client import BlockChainServiceMock, GAS_LIMIT_HEX

log = getLogger(__name__)  # pylint: disable=invalid-name

DEFAULT_DEPOSIT = 2 ** 240
""" An arbitrary initial balance for each channel in the test network. """

DEFAULT_PASSPHRASE = 'notsosecret'
""" Geth account default passphrase used for the testing account. """

DEFAULT_BALANCE = str(denoms.turing * 1)

CHAIN = object()
""" Flag used by create_sequential_network to create a network that does make a
loop.
"""


def check_channel(app1, app2, netting_channel_address):
    netcontract1 = app1.raiden.chain.netting_channel(netting_channel_address)
    netcontract2 = app2.raiden.chain.netting_channel(netting_channel_address)

    assert netcontract1.isopen()
    assert netcontract2.isopen()

    assert netcontract1.detail(app1.raiden.address) == netcontract2.detail(app1.raiden.address)
    assert netcontract2.detail(app2.raiden.address) == netcontract1.detail(app2.raiden.address)

    app1_details = netcontract1.detail(app1.raiden.address)
    app2_details = netcontract2.detail(app2.raiden.address)

    assert app1_details['our_address'] == app2_details['partner_address']
    assert app1_details['partner_address'] == app2_details['our_address']

    assert app1_details['our_balance'] == app2_details['partner_balance']
    assert app1_details['partner_balance'] == app2_details['our_balance']


def create_app(privkey_bin, chain, discovery, transport_class, port, host='127.0.0.1'):  # pylint: disable=too-many-arguments
    ''' Instantiates an Raiden app with the given configuration. '''
    config = copy.deepcopy(App.default_config)

    config['port'] = port
    config['host'] = host
    config['privkey'] = privkey_bin

    return App(
        config,
        chain,
        discovery,
        transport_class,
    )


def setup_channels(asset_address, app_pairs, deposit, settle_timeout):  # pylint: disable=too-many-locals
    for first, second in app_pairs:
        manager = first.raiden.chain.manager_by_asset(asset_address)

        netcontract_address = manager.new_netting_channel(
            first.raiden.address,
            second.raiden.address,
            settle_timeout,
        )

        # use each app's own chain because of the private key / local signing
        for app in [first, second]:
            asset = app.raiden.chain.asset(asset_address)
            netting_channel = app.raiden.chain.netting_channel(netcontract_address)
            previous_balance = asset.balance_of(app.raiden.address)

            assert previous_balance >= deposit

            asset.approve(netcontract_address, deposit)
            netting_channel.deposit(app.raiden.address, deposit)

            new_balance = asset.balance_of(app.raiden.address)

            assert previous_balance - deposit == new_balance

            # netting contract does allow settle time lower than 30
            contract_settle_timeout = netting_channel.settle_timeout()
            assert contract_settle_timeout == max(30, settle_timeout)

        check_channel(
            first,
            second,
            netcontract_address,
        )

        first_netting_channel = first.raiden.chain.netting_channel(netcontract_address)
        second_netting_channel = second.raiden.chain.netting_channel(netcontract_address)

        details1 = first_netting_channel.detail(first.raiden.address)
        details2 = second_netting_channel.detail(second.raiden.address)

        assert details1['our_balance'] == deposit
        assert details1['partner_balance'] == deposit
        assert details2['our_balance'] == deposit
        assert details2['partner_balance'] == deposit


def network_with_minimum_channels(apps, channels_per_node):
    """ Return the channels that should be created so that each app has at
    least `channels_per_node` with the other apps.

    Yields a two-tuple (app1, app2) that must be connected to respect
    `channels_per_node`. Any preexisting channels will be ignored, so the nodes
    might end up with more channels open than `channels_per_node`.
    """
    # pylint: disable=too-many-locals
    if channels_per_node > len(apps):
        raise ValueError("Can't create more channels than nodes")

    # If we use random nodes we can hit some edge cases, like the
    # following:
    #
    #  node | #channels
    #   A   |    0
    #   B   |    1  D-B
    #   C   |    1  D-C
    #   D   |    2  D-C D-B
    #
    # B and C have one channel each, and they do not a channel
    # between them, if in this iteration either app is the current
    # one and random choose the other to connect, A will be left
    # with no channels. In this scenario we need to force the use
    # of the node with the least number of channels.

    unconnected_apps = dict()
    channel_count = dict()

    # assume that the apps don't have any connection among them
    for curr_app in apps:
        all_apps = list(apps)
        all_apps.remove(curr_app)
        unconnected_apps[curr_app.raiden.address] = all_apps
        channel_count[curr_app.raiden.address] = 0

    # Create `channels_per_node` channels for each asset in each app
    # for asset_address, curr_app in product(assets_list, sorted(apps, key=sort_by_address)):

    # sorting the apps and use the next n apps to make a channel to avoid edge
    # cases
    for curr_app in sorted(apps, key=lambda app: app.raiden.address):
        available_apps = unconnected_apps[curr_app.raiden.address]

        while channel_count[curr_app.raiden.address] < channels_per_node:
            least_connect = sorted(
                available_apps,
                key=lambda app: channel_count[app.raiden.address]  # pylint: disable=cell-var-from-loop
            )[0]

            channel_count[curr_app.raiden.address] += 1
            available_apps.remove(least_connect)

            channel_count[least_connect.raiden.address] += 1
            unconnected_apps[least_connect.raiden.address].remove(curr_app)

            yield curr_app, least_connect


def create_network(private_keys, assets_addresses, registry_address,  # pylint: disable=too-many-arguments
                   channels_per_node, deposit, settle_timeout, transport_class,
                   blockchain_service_class):
    """ Initialize a local test network using the UDP protocol.

    Note:
        The generated network will use two subnets, 127.0.0.10 and 127.0.0.11,
        for this test to work both virtual interfaces must be created prior to
        the test execution::

            ifconfig lo:0 127.0.0.10
            ifconfig lo:1 127.0.0.11
    """
    # pylint: disable=too-many-locals

    random.seed(1337)
    num_nodes = len(private_keys)

    if channels_per_node > num_nodes:
        raise ValueError("Can't create more channels than nodes")

    # if num_nodes it is not even
    half_of_nodes = int(floor(len(private_keys) / 2))

    # globals
    discovery = Discovery()

    # The mock needs to be atomic since all app's will use the same instance,
    # for the real application the syncronization is done by the JSON-RPC
    # server
    blockchain_service_class = blockchain_service_class or BlockChainServiceMock

    # Each app instance is a Node in the network
    apps = []
    for idx, privatekey_bin in enumerate(private_keys):

        # TODO: check if the loopback interfaces exists
        # split the nodes into two different networks
        if idx > half_of_nodes:
            host = '127.0.0.11'
        else:
            host = '127.0.0.10'

        nodeid = privtoaddr(privatekey_bin)
        port = INITIAL_PORT + idx

        discovery.register(nodeid, host, port)

        jsonrpc_client = JSONRPCClient(
            privkey=privatekey_bin,
            print_communication=False,
        )
        blockchain_service = blockchain_service_class(
            jsonrpc_client,
            registry_address,
        )

        app = create_app(
            privatekey_bin,
            blockchain_service,
            discovery,
            transport_class,
            port=port,
            host=host,
        )

        apps.append(app)

    for asset in assets_addresses:
        if channels_per_node == CHAIN:
            app_channels = list(zip(apps[:-1], apps[1:]))
        else:
            app_channels = list(network_with_minimum_channels(apps, channels_per_node))

        setup_channels(
            asset,
            app_channels,
            deposit,
            settle_timeout,
        )

    for app in apps:
        for asset_address in app.raiden.chain.default_registry.asset_addresses():
            manager = app.raiden.chain.manager_by_asset(asset_address)
            app.raiden.register_channel_manager(manager)

    return apps


def create_sequential_network(private_keys, asset_address, registry_address,  # pylint: disable=too-many-arguments
                              channels_per_node, deposit, settle_timeout,
                              transport_class, blockchain_service_class):
    """ Create a fully connected network with `num_nodes`, the nodes are
    connect sequentially.

    Returns:
        A list of apps of size `num_nodes`, with the property that every
        sequential pair in the list has an open channel with `deposit` for each
        participant.
    """
    # pylint: disable=too-many-locals

    random.seed(42)

    host = '127.0.0.10'
    num_nodes = len(private_keys)

    if num_nodes < 2:
        raise ValueError('cannot create a network with less than two nodes')

    if channels_per_node not in (0, 1, 2, CHAIN):
        raise ValueError('can only create networks with 0, 1, 2 or CHAIN channels')

    discovery = Discovery()
    blockchain_service_class = blockchain_service_class or BlockChainServiceMock

    apps = []
    for idx, privatekey_bin in enumerate(private_keys):
        port = INITIAL_PORT + idx
        nodeid = privtoaddr(privatekey_bin)

        discovery.register(nodeid, host, port)

        jsonrpc_client = JSONRPCClient(
            privkey=privatekey_bin,
            print_communication=False,
        )
        blockchain_service = blockchain_service_class(
            jsonrpc_client,
            registry_address,
        )

        app = create_app(
            privatekey_bin,
            blockchain_service,
            discovery,
            transport_class,
            port=port,
            host=host,
        )
        apps.append(app)

    if channels_per_node == 0:
        app_channels = list()

    if channels_per_node == 1:
        every_two = iter(apps)
        app_channels = list(zip(every_two, every_two))

    if channels_per_node == 2:
        app_channels = list(zip(apps, apps[1:] + apps[0]))

    if channels_per_node == CHAIN:
        app_channels = list(zip(apps[:-1], apps[1:]))

    setup_channels(
        asset_address,
        app_channels,
        deposit,
        settle_timeout,
    )

    for app in apps:
        for asset_address in app.raiden.chain.default_registry.asset_addresses():
            manager = app.raiden.chain.manager_by_asset(asset_address)
            app.raiden.register_channel_manager(manager)

    return apps


def create_hydrachain_cluster(private_keys, hydrachain_private_keys, p2p_base_port, base_datadir):
    """ Initializes a hydrachain network used for testing. """
    # pylint: disable=too-many-locals
    from hydrachain.app import services, start_app, HPCApp
    import pyethapp.config as konfig

    def privkey_to_uri(private_key):
        host = b'0.0.0.0'
        pubkey = privtopub(private_key)
        return host_port_pubkey_to_uri(host, p2p_base_port, pubkey)

    account_addresses = [
        privtoaddr(priv)
        for priv in private_keys
    ]

    alloc = {
        encode_hex(address): {
            'balance': DEFAULT_BALANCE,
        }
        for address in account_addresses
    }

    genesis = {
        'nonce': '0x00006d6f7264656e',
        'difficulty': '0x20000',
        'mixhash': '0x00000000000000000000000000000000000000647572616c65787365646c6578',
        'coinbase': '0x0000000000000000000000000000000000000000',
        'timestamp': '0x00',
        'parentHash': '0x0000000000000000000000000000000000000000000000000000000000000000',
        'extraData': '0x',
        'gasLimit': GAS_LIMIT_HEX,
        'alloc': alloc,
    }

    bootstrap_nodes = [
        privkey_to_uri(hydrachain_private_keys[0]),
    ]

    validators_addresses = [
        privtoaddr(private_key)
        for private_key in hydrachain_private_keys
    ]

    all_apps = []
    for number, private_key in enumerate(hydrachain_private_keys):
        config = konfig.get_default_config(services + [HPCApp])
        config = update_config_from_genesis_json(config, genesis)

        datadir = os.path.join(base_datadir, str(number))
        konfig.setup_data_dir(datadir)

        account = Account.new(
            password='',
            key=private_key,
        )

        config['data_dir'] = datadir
        config['hdc']['validators'] = validators_addresses
        config['node']['privkey_hex'] = encode_hex(private_key)
        config['jsonrpc']['listen_port'] += number
        config['client_version_string'] = 'NODE{}'.format(number)

        # setting to 0 so that the CALLCODE opcode works at the start of the
        # network
        config['eth']['block']['HOMESTEAD_FORK_BLKNUM'] = 0

        config['discovery']['bootstrap_nodes'] = bootstrap_nodes
        config['discovery']['listen_port'] = p2p_base_port + number

        config['p2p']['listen_port'] = p2p_base_port + number
        config['p2p']['min_peers'] = min(10, len(hydrachain_private_keys) - 1)
        config['p2p']['max_peers'] = len(hydrachain_private_keys) * 2

        # only one of the nodes should have the Console service running
        if number != 0 and Console.name not in config['deactivated_services']:
            config['deactivated_services'].append(Console.name)

        hydrachain_app = start_app(config, accounts=[account])
        all_apps.append(hydrachain_app)

    return all_apps


def geth_to_cmd(node, datadir=None):
    """
    Transform a node configuration into a cmd-args list for `subprocess.Popen`.

    Args:
        node (dict): a node configuration
        datadir (str): the node's datadir

    Return:
        List[str]: cmd-args list
    """
    node_config = [
        'nodekeyhex',
        'port',
        'rpcport',
        'bootnodes',
        'minerthreads',
        'unlock'
    ]

    cmd = ['geth']

    for config in node_config:
        if config in node:
            value = node[config]
            cmd.extend(['--{}'.format(config), str(value)])

    if 'minerthreads' in node:
        cmd.extend(['--mine', '--etherbase', '0'])

    if datadir:
        cmd.extend(['--datadir', datadir])
        cmd.extend(['--genesis', os.path.join(datadir, 'genesis.json')])

    cmd.extend([
        '--nodiscover',
        '--dev',
        '--ipcdisable',
        '--rpc',
        '--jitvm=false',
        '--networkid', '627',
    ])

    return cmd


def geth_create_account(datadir):
    """
    Create an account in `datadir` -- since we're not interested
    in the rewards, we don't care about the created address.

    Args:
        datadir (str): the datadir in which the account is created
    """

    create = Popen(
        shlex.split('geth --datadir {} account new'.format(datadir)),
        stdin=PIPE,
        universal_newlines=True,
    )
    create.stdin.write(DEFAULT_PASSPHRASE + os.linesep)
    time.sleep(.1)
    create.stdin.write(DEFAULT_PASSPHRASE + os.linesep)
    create.communicate()
    assert create.returncode == 0


def create_geth_cluster(private_keys, geth_private_keys, p2p_base_port, base_datadir):  # pylint: disable=too-many-locals,too-many-statements
    start_rpcport = 4000

    account_addresses = [
        privtoaddr(key)
        for key in private_keys
    ]

    alloc = {
        encode_hex(address): {
            'balance': DEFAULT_BALANCE,
        }
        for address in account_addresses
    }

    genesis = {
        'nonce': '0x0000000000000042',
        'mixhash': '0x0000000000000000000000000000000000000000000000000000000000000000',
        'difficulty': '0x4000',
        'coinbase': '0x0000000000000000000000000000000000000000',
        'timestamp': '0x00',
        'parentHash': '0x0000000000000000000000000000000000000000000000000000000000000000',
        'extraData': 'raiden',
        'gasLimit': '0xffffffff',
        'alloc': alloc,
    }

    nodes_configuration = []
    for pos, key in enumerate(geth_private_keys):
        config = dict()

        # make the first node miner
        if pos == 0:
            config['minerthreads'] = 1  # conservative
            config['unlock'] = 0

        config['nodekey'] = key
        config['nodekeyhex'] = encode_hex(key)
        config['pub'] = encode_hex(privtopub(key))
        config['address'] = privtoaddr(key)
        config['port'] = p2p_base_port + pos
        config['rpcport'] = start_rpcport + pos
        config['enode'] = 'enode://{pub}@127.0.0.1:{port}'.format(
            pub=config['pub'],
            port=config['port'],
        )
        config['bootnodes'] = ','.join(node['enode'] for node in nodes_configuration)

        nodes_configuration.append(config)

    cmds = []
    for config in nodes_configuration:
        nodedir = os.path.join(base_datadir, config['nodekeyhex'])
        os.makedirs(nodedir)
        with open(os.path.join(nodedir, 'genesis.json'), 'w') as handler:
            json.dump(genesis, handler)

            if 'minerthreads' in config:
                geth_create_account(nodedir)

            cmds.append(geth_to_cmd(config, datadir=nodedir))

    processes = []
    for cmd in cmds:
        if '--unlock' in cmd:
            proc = Popen(cmd, universal_newlines=True, stdin=PIPE)
            # --password wont work, write password to unlock
            proc.stdin.write(DEFAULT_PASSPHRASE + os.linesep)
            processes.append(proc)
        else:
            processes.append(Popen(cmd))
            print('spawned process')


    return processes