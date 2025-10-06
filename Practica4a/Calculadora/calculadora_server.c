#include "calculadora.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <arpa/inet.h>
#include <rpc/pmap_clnt.h>
#include <sys/socket.h>
#include <netinet/in.h>

/* Obtener IP del cliente */
void obtener_ip_cliente(struct svc_req *rqstp, char *ip_str) {
    struct sockaddr_in *addr = svc_getcaller(rqstp->rq_xprt);
    inet_ntop(AF_INET, &(addr->sin_addr), ip_str, INET_ADDRSTRLEN);
}

/* Registrar operación */
void registrar_operacion(const char *operacion, dupla_int *nums, const char *ip) {
    time_t ahora;
    char timestamp[26];
    FILE *log;

    time(&ahora);
    strcpy(timestamp, ctime(&ahora));
    timestamp[strlen(timestamp) - 1] = '\0';

    log = fopen("calculadora.log", "a");
    if (log != NULL) {
        fprintf(log, "[%s] IP: %s | Operación: %s | a=%d, b=%d\n",
                timestamp, ip, operacion, nums->a, nums->b);
        fclose(log);
    }

    printf("[%s] IP: %s realizó: %s con a=%d, b=%d\n",
           timestamp, ip, operacion, nums->a, nums->b);
}

/* SUMA */
int *suma_1_svc(dupla_int *argp, struct svc_req *rqstp) {
    static int resultado;
    char ip_cliente[INET_ADDRSTRLEN];
    obtener_ip_cliente(rqstp, ip_cliente);
    registrar_operacion("SUMA", argp, ip_cliente);
    resultado = argp->a + argp->b;
    return &resultado;
}

/* RESTA */
int *resta_1_svc(dupla_int *argp, struct svc_req *rqstp) {
    static int resultado;
    char ip_cliente[INET_ADDRSTRLEN];
    obtener_ip_cliente(rqstp, ip_cliente);
    registrar_operacion("RESTA", argp, ip_cliente);
    resultado = argp->a - argp->b;
    return &resultado;
}

/* MULTIPLICACIÓN */
int *multiplica_1_svc(dupla_int *argp, struct svc_req *rqstp) {
    static int resultado;
    char ip_cliente[INET_ADDRSTRLEN];
    obtener_ip_cliente(rqstp, ip_cliente);
    registrar_operacion("MULTIPLICACIÓN", argp, ip_cliente);
    resultado = argp->a * argp->b;
    return &resultado;
}

/* DIVISIÓN */
float *divide_1_svc(dupla_int *argp, struct svc_req *rqstp) {
    static float resultado;
    char ip_cliente[INET_ADDRSTRLEN];
    obtener_ip_cliente(rqstp, ip_cliente);
    registrar_operacion("DIVISIÓN", argp, ip_cliente);
    if (argp->b == 0) {
        resultado = 0.0;
    } else {
        resultado = (float)argp->a / (float)argp->b;
    }
    return &resultado;
}

