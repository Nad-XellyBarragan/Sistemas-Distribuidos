import java.net.*;
import java.io.*;

public class ServidorTexto {
    public static void main(String[] args) throws IOException {
        
        if (args.length != 1) {
            System.err.println("Uso: java ServidorTexto <puerto>");
            System.exit(1);
        }
        
        int puerto = Integer.parseInt(args[0]);
        
        try (
            ServerSocket serverSocket = new ServerSocket(puerto);
        ) {
            System.out.println("Servidor de texto escuchando en puerto " + puerto);
            
            while (true) {
                try (
                    Socket clientSocket = serverSocket.accept();
                    PrintWriter out = new PrintWriter(clientSocket.getOutputStream(), true);
                    BufferedReader in = new BufferedReader(
                        new InputStreamReader(clientSocket.getInputStream()));
                ) {
                    System.out.println("Cliente conectado desde: " + 
                        clientSocket.getInetAddress().getHostAddress());
                    
                    String mensajeCliente;
                    while ((mensajeCliente = in.readLine()) != null) {
                        System.out.println("Cliente dice: " + mensajeCliente);
                        
                        String respuesta;
                        if (mensajeCliente.equalsIgnoreCase("Hola")) {
                            respuesta = "Hola que tal";
                        } else if (mensajeCliente.equalsIgnoreCase("adios")) {
                            respuesta = "Hasta luego!";
                            out.println(respuesta);
                            System.out.println("Servidor responde: " + respuesta);
                            break;
                        } else {
                            respuesta = "Recibido: " + mensajeCliente;
                        }
                        
                        out.println(respuesta);
                        System.out.println("Servidor responde: " + respuesta);
                    }
                    
                    System.out.println("Cliente desconectado\n");
                    
                } catch (IOException e) {
                    System.err.println("Error con cliente: " + e.getMessage());
                }
            }
            
        } catch (IOException e) {
            System.err.println("Error en puerto " + puerto + ": " + e.getMessage());
        }
    }
}