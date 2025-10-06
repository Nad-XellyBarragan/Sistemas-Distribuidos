// rand_server.c
#include <stdlib.h>
#include <time.h>
#include <stdio.h>
#include <arpa/inet.h>
#include "rand.h"

void *inicializa_random_1_svc(long *argp, struct svc_req *rqstp)
{
    static char dummy;
    long seed = *argp;
    if (seed == 0) seed = (long)time(NULL);
    
    srand((unsigned int)seed);
    
    // Log de la operación
    struct sockaddr_in *addr = svc_getcaller(rqstp->rq_xprt);
    printf("[SERVIDOR] Cliente %s:%d - Inicializa semilla: %ld\n",
           inet_ntoa(addr->sin_addr),
           ntohs(addr->sin_port),
           seed);
    fflush(stdout);
    
    return (void *)&dummy;
}

double *obtiene_siguiente_random_1_svc(void *argp, struct svc_req *rqstp)
{
    static double result;
    result = (double)rand() / (double)RAND_MAX;
    
    // Log de la operación
    struct sockaddr_in *addr = svc_getcaller(rqstp->rq_xprt);
    printf("[SERVIDOR] Cliente %s:%d - Random generado: %f\n",
           inet_ntoa(addr->sin_addr),
           ntohs(addr->sin_port),
           result);
    fflush(stdout);
    
    return &result;
}
