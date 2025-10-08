#include <rpc/rpc.h>
#include <stdio.h>
#include <stdlib.h>
#include "calculadora.h"

int main(int argc, char *argv[])
{
    CLIENT *clnt;
    char *host;
    dupla_int numeros;
    int *res_int;
    float *res_float;

    if (argc != 4) {
        printf("Uso: %s <host> <num1> <num2>\n", argv[0]);
        return 1;
    }

    host = argv[1];
    numeros.a = atoi(argv[2]);
    numeros.b = atoi(argv[3]);

    clnt = clnt_create(host, CALCULADORA_PROG, CALCULADORA_VERS, "tcp");
    if (clnt == NULL) {
        clnt_pcreateerror(host);
        exit(1);
    }

    res_int = suma_1(&numeros, clnt);
    if (res_int != NULL)
        printf("Suma: %d + %d = %d\n", numeros.a, numeros.b, *res_int);

    res_int = resta_1(&numeros, clnt);
    if (res_int != NULL)
        printf("Resta: %d - %d = %d\n", numeros.a, numeros.b, *res_int);

    res_int = multiplica_1(&numeros, clnt);
    if (res_int != NULL)
        printf("Multiplicación: %d * %d = %d\n", numeros.a, numeros.b, *res_int);

    res_float = divide_1(&numeros, clnt);
    if (res_float != NULL) {
        if (numeros.b == 0) {
            printf("División: ERROR (división por cero)\n");
        } else {
            printf("División: %d / %d = %.2f\n", numeros.a, numeros.b, *res_float);
        }
    }

    clnt_destroy(clnt);
    return 0;
}

