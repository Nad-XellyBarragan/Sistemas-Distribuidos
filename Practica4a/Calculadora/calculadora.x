struct dupla_int {
    int a;
    int b;
};

program CALCULADORA_PROG {
    version CALCULADORA_VERS {
        int SUMA(dupla_int) = 1;
        int RESTA(dupla_int) = 2;
        int MULTIPLICA(dupla_int) = 3;
        float DIVIDE(dupla_int) = 4;
    } = 1;
} = 0x23451111;

