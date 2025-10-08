import java.io.*;
import java.net.*;
import java.util.Scanner;

public class ClienteNumeros {
    public static void main(String[] args) {
        
        if (args.length != 2) {
            System.err.println("Uso: java ClienteNumeros <IP_servidor> <puerto>");
            System.err.println("Ejemplo: java ClienteNumeros 127.0.0.1 6000");
            System.exit(1);
        }
        
        String host = args[0];
        int puerto = Integer.parseInt(args[1]);
        
        try (
            Socket socket = new Socket(host, puerto);
            PrintWriter out = new PrintWriter(socket.getOutputStream(), true);
            BufferedReader in = new BufferedReader(
                new InputStreamReader(socket.getInputStream()));
            Scanner teclado = new Scanner(System.in);
        ) {
            System.out.println("Conectado al servidor " + host + ":" + puerto);
            System.out.println("Ingresa numeros enteros (0 para terminar)\n");
            
            while (true) {
                System.out.print("Ingresa un numero: ");
                
                int numero;
                try {
                    numero = teclado.nextInt();
                } catch (Exception e) {
                    System.out.println("Error: Debes ingresar un numero entero");
                    teclado.nextLine(); // Limpiar buffer
                    continue;
                }
                
                // Enviar numero
                out.println(numero);
                System.out.println("Enviado: " + numero);
                
                // Recibir respuesta
                String respuesta = in.readLine();
                if (respuesta != null) {
                    int resultado = Integer.parseInt(respuesta.trim());
                    System.out.println("Servidor responde: " + resultado);
                    System.out.println();
                    
                    if (numero == 0) {
                        System.out.println("Cerrando conexion...");
                        break;
                    }
                } else {
                    System.out.println("Servidor cerro la conexion");
                    break;
                }
            }
            
        } catch (UnknownHostException e) {
            System.err.println("No se puede conectar a " + host);
        } catch (IOException e) {
            System.err.println("Error de E/S: " + e.getMessage());
        }
    }
}