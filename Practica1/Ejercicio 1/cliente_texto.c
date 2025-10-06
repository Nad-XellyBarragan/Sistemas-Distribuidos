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
    SOCKET sock;
    struct sockaddr_in servidor;
    char mensaje[1024];
    char respuesta[1024];
    int bytes_recibidos;
    
    #ifdef _WIN32
    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2,2), &wsa) != 0) {
        printf("Error al inicializar Winsock: %d\n", WSAGetLastError());
        return 1;
    }
    #endif
    
    if (argc != 3) {
        printf("Uso: %s <IP_servidor> <puerto>\n", argv[0]);
        printf("Ejemplo: %s 127.0.0.1 5000\n", argv[0]);
        return 1;
    }
    
    // Crear socket
    sock = socket(AF_INET, SOCK_STREAM, 0);
    if (sock == INVALID_SOCKET) {
        printf("Error al crear socket\n");
        return 1;
    }
    
    // Configurar servidor
    servidor.sin_family = AF_INET;
    servidor.sin_port = htons(atoi(argv[2]));
    servidor.sin_addr.s_addr = inet_addr(argv[1]);
    
    // Conectar
    printf("Conectando a %s:%s...\n", argv[1], argv[2]);
    if (connect(sock, (struct sockaddr *)&servidor, sizeof(servidor)) < 0) {
        printf("Error al conectar con el servidor\n");
        close(sock);
        return 1;
    }
    
    printf("Conectado al servidor!\n");
    printf("Escribe 'adios' para terminar\n\n");
    
    while (1) {
        printf("Tu mensaje: ");
        fgets(mensaje, sizeof(mensaje), stdin);
        
        // Quitar salto de línea
        mensaje[strcspn(mensaje, "\n")] = 0;
        
        // Verificar si quiere salir
        if (strlen(mensaje) == 0) {
            continue;
        }
        
        // Enviar mensaje (agregar \n al final)
        strcat(mensaje, "\n");
        if (send(sock, mensaje, strlen(mensaje), 0) < 0) {
            printf("Error al enviar mensaje\n");
            break;
        }
        
        // Si envió "adios", salir después de recibir respuesta
        int salir = (strncmp(mensaje, "adios", 5) == 0 || strncmp(mensaje, "Adios", 5) == 0);
        
        // Recibir respuesta
        bytes_recibidos = recv(sock, respuesta, sizeof(respuesta)-1, 0);
        if (bytes_recibidos > 0) {
            respuesta[bytes_recibidos] = '\0';
            printf("Servidor responde: %s\n", respuesta);
        } else {
            printf("Servidor cerro la conexion\n");
            break;
        }
        
        if (salir) {
            break;
        }
    }
    
    close(sock);
    
    #ifdef _WIN32
    WSACleanup();
    #endif
    
    printf("\nConexion cerrada.\n");
    return 0;
}