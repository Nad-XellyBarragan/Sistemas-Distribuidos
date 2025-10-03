#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
    #include <winsock2.h>
    #pragma comment(lib, "ws2_32.lib")
    #define close closesocket
#else
    #include <sys/socket.h>
    #include <arpa/inet.h>
    #include <unistd.h>
    #define SOCKET int
    #define INVALID_SOCKET -1
    #define SOCKET_ERROR -1
#endif

int main(int argc, char *argv[]) {
    SOCKET server_sock, client_sock;
    struct sockaddr_in servidor, cliente;
    int c, numero, resultado;
    char buffer[1024];
    int bytes_recibidos;
    
    #ifdef _WIN32
    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2,2), &wsa) != 0) {
        printf("Error al inicializar Winsock: %d\n", WSAGetLastError());
        return 1;
    }
    #endif
    
    if (argc != 2) {
        printf("Uso: %s <puerto>\n", argv[0]);
        return 1;
    }
    
    // Crear socket
    server_sock = socket(AF_INET, SOCK_STREAM, 0);
    if (server_sock == INVALID_SOCKET) {
        printf("Error al crear socket\n");
        return 1;
    }
    
    // Configurar servidor
    servidor.sin_family = AF_INET;
    servidor.sin_addr.s_addr = INADDR_ANY;
    servidor.sin_port = htons(atoi(argv[1]));
    
    // Bind
    if (bind(server_sock, (struct sockaddr *)&servidor, sizeof(servidor)) == SOCKET_ERROR) {
        printf("Error en bind\n");
        close(server_sock);
        return 1;
    }
    
    // Listen
    listen(server_sock, 3);
    printf("Servidor de numeros escuchando en puerto %s\n", argv[1]);
    printf("Esperando conexiones...\n\n");
    
    c = sizeof(struct sockaddr_in);
    
    while (1) {
        // Accept
        client_sock = accept(server_sock, (struct sockaddr *)&cliente, &c);
        if (client_sock == INVALID_SOCKET) {
            printf("Error al aceptar conexion\n");
            continue;
        }
        
        printf("Cliente conectado desde: %s:%d\n", 
            inet_ntoa(cliente.sin_addr), ntohs(cliente.sin_port));
        
        // Procesar numeros
        while (1) {
            bytes_recibidos = recv(client_sock, buffer, sizeof(buffer)-1, 0);
            
            if (bytes_recibidos <= 0) {
                printf("Cliente desconectado\n\n");
                break;
            }
            
            buffer[bytes_recibidos] = '\0';
            numero = atoi(buffer);
            
            printf("Cliente envio: %d\n", numero);
            
            if (numero == 0) {
                printf("Cliente envio 0, cerrando conexion\n\n");
                resultado = 0;
                sprintf(buffer, "%d\n", resultado);
                send(client_sock, buffer, strlen(buffer), 0);
                break;
            }
            
            resultado = numero + 1;
            printf("Servidor responde: %d\n\n", resultado);
            
            sprintf(buffer, "%d\n", resultado);
            send(client_sock, buffer, strlen(buffer), 0);
        }
        
        close(client_sock);
    }
    
    close(server_sock);
    
    #ifdef _WIN32
    WSACleanup();
    #endif
    
    return 0;
}