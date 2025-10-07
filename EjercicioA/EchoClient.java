import java.io.*;
import java.net.*;

public class EchoClient {
    public static void main(String[] args) throws Exception {
        String host = (args.length > 0) ? args[0] : "127.0.0.1";
        int port = (args.length > 1) ? Integer.parseInt(args[1]) : 5000;

        try (Socket socket = new Socket(host, port);
             BufferedReader console = new BufferedReader(new InputStreamReader(System.in));
             BufferedReader in = new BufferedReader(new InputStreamReader(socket.getInputStream()));
             PrintWriter out = new PrintWriter(socket.getOutputStream(), true)) {

            System.out.println("Conectado a " + host + ":" + port + ". Escribe y presiona ENTER (\"quit\" para salir).");

            String line;
            while ((line = console.readLine()) != null) {
                out.println(line);
                String resp = in.readLine(); // recibe el eco (tras ~3s)
                System.out.println(resp);
                if ("quit".equalsIgnoreCase(line.trim())) break;
            }
        }
    }
}
