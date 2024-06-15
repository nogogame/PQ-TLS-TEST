import os
import subprocess
import pathlib
import psutil
import shutil
import time

SERVER_START_ATTEMPTS = 60

BSSL_SHIM = os.path.join('boringssl', 'build', 'ssl', 'test', 'bssl_shim')
BSSL = os.path.join('boringssl', 'build', 'tool', 'bssl')
OSSL = os.path.join('apps', 'openssl')

TLS1_3_VERSION=772 #0x0304

key_exchanges = [
##### OQS_TEMPLATE_FRAGMENT_KEX_ALGS_START
    # post-quantum key exchanges
    'frodo640aes','frodo640shake','frodo976aes','frodo976shake','frodo1344aes','frodo1344shake','kyber512','kyber768','kyber1024','bikel1','bikel3','bikel5','hqc128','hqc192','hqc256','sntrup761','nttru','nttruref'
    # post-quantum + classical key exchanges
    'p256_frodo640aes','p256_frodo640shake','p384_frodo976aes','p384_frodo976shake','p521_frodo1344aes','p521_frodo1344shake','p256_kyber512','p384_kyber768','p521_kyber1024','p256_bikel1','p384_bikel3','p521_bikel5','p256_hqc128','p384_hqc192','p521_hqc256','p256_sntrup761','p384_nttru','p384_nttruref',
##### OQS_TEMPLATE_FRAGMENT_KEX_ALGS_END
]

signatures = [
##### OQS_TEMPLATE_FRAGMENT_PQ_SIG_ALGS_START
    'dilithium2',
    'dilithium3',
    'dilithium5',
    'falcon512',
    'falcon1024',
    'sphincssha2128fsimple',
    'sphincssha2128ssimple',
    'sphincssha2192fsimple',
    'sphincsshake128fsimple',
##### OQS_TEMPLATE_FRAGMENT_PQ_SIG_ALGS_END
]

def run_subprocess(command, working_dir='.', expected_returncode=0, input=None):
    """
    Helper function to run a shell command and report success/failure
    depending on the exit status of the shell command.
    """

    # Note we need to capture stdout/stderr from the subprocess,
    # then print it, which pytest will then capture and
    # buffer appropriately
    print(working_dir + " > " + " ".join(command))
    result = subprocess.run(
        command,
        input=input,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=working_dir,
    )
    if result.returncode != expected_returncode:
        print(result.stdout.decode('utf-8'))
        assert False, "Got unexpected return code {}".format(result.returncode)
    return result.stdout.decode('utf-8')


def gen_openssl_keys(ossl, sig_alg, test_artifacts_dir, filename_prefix):
    pathlib.Path(test_artifacts_dir).mkdir(parents=True, exist_ok=True)

    CA_cert_path = os.path.join(test_artifacts_dir, '{}_{}_CA.crt'.format(filename_prefix, sig_alg))
    server_cert_path = os.path.join(test_artifacts_dir, '{}_{}_srv.crt'.format(filename_prefix, sig_alg))

    ossl_config = os.path.join('apps', 'openssl.cnf')

    run_subprocess([ossl, 'req', '-x509', '-new',
                                 '-newkey', sig_alg,
                                 '-keyout', os.path.join(test_artifacts_dir, '{}_{}_CA.key'.format(filename_prefix, sig_alg)),
                                 '-out', CA_cert_path,
                                 '-nodes',
                                     '-subj', '/CN=oqstest_CA',
                                     '-days', '365',
                                 '-config', ossl_config])
    run_subprocess([ossl, 'req', '-new',
                          '-newkey', sig_alg,
                          '-keyout', os.path.join(test_artifacts_dir, '{}_{}_srv.key'.format(filename_prefix, sig_alg)),
                          '-out', os.path.join(test_artifacts_dir, '{}_{}_srv.csr'.format(filename_prefix, sig_alg)),
                          '-nodes',
                              '-subj', '/CN=oqstest_server',
                          '-config', ossl_config])
    run_subprocess([ossl, 'x509', '-req',
                                  '-in', os.path.join(test_artifacts_dir, '{}_{}_srv.csr'.format(filename_prefix, sig_alg)),
                                  '-out', server_cert_path,
                                  '-CA', CA_cert_path,
                                  '-CAkey', os.path.join(test_artifacts_dir, '{}_{}_CA.key'.format(filename_prefix, sig_alg)),
                                  '-CAcreateserial',
                                  '-days', '365'])

    with open(os.path.join(test_artifacts_dir, '{}_{}_cert_chain'.format(filename_prefix, sig_alg)),'wb') as out_file:
        for f in [server_cert_path, CA_cert_path]:
            with open(f, 'rb') as in_file:
                shutil.copyfileobj(in_file, out_file)

def start_server(client_type, test_artifacts_dir, sig_alg, worker_id):
    gen_openssl_keys(OSSL, sig_alg, test_artifacts_dir, worker_id)

    if client_type == "ossl":
        server_command = [BSSL, 'server',
                                '-accept', '0',
                                '-cert', os.path.join(test_artifacts_dir, '{}_{}_srv.crt'.format(worker_id, sig_alg)),
                                '-key', os.path.join(test_artifacts_dir, '{}_{}_srv.key'.format(worker_id, sig_alg)),
                                '-loop']
    elif client_type == "bssl":
        server_command = [OSSL, 's_server',
                                '-cert', os.path.join(test_artifacts_dir, '{}_{}_srv.crt'.format(worker_id, sig_alg)),
                                '-key', os.path.join(test_artifacts_dir, '{}_{}_srv.key'.format(worker_id, sig_alg)),
                                '-CAfile', os.path.join(test_artifacts_dir, '{}_{}_CA.crt'.format(worker_id, sig_alg)),
                                '-tls1_3',
                                '-quiet',
                                '-accept', '0']

    print(". > " + " ".join(server_command))
    server = subprocess.Popen(server_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    server_info = psutil.Process(server.pid)

    # Try SERVER_START_ATTEMPTS times to see
    # what port the server is bound to.
    server_start_attempt = 1
    while server_start_attempt <= SERVER_START_ATTEMPTS:
        if server_info.connections():
            break
        else:
            server_start_attempt += 1
            time.sleep(2)
    server_port = str(server_info.connections()[0].laddr.port)

    if client_type == "ossl":
        client_command = [OSSL, 's_client', '-connect', 'localhost:{}'.format(server_port)]
    elif client_type == "bssl":
        client_command = [BSSL_SHIM, '-port', server_port, '-shim-shuts-down']

    # Check SERVER_START_ATTEMPTS times to see
    # if the server is responsive.
    server_start_attempt = 1
    while server_start_attempt <= SERVER_START_ATTEMPTS:
        result = subprocess.run(client_command, input='Q'.encode(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if result.returncode == 0:
            break
        else:
            server_start_attempt += 1
            time.sleep(2)

    if server_start_attempt > SERVER_START_ATTEMPTS:
        raise Exception('Cannot start server')

    return server, server_port
