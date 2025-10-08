import java.io.*;
import java.net.*;
import java.util.concurrent.*;

public class EchoMultiServer {

    private final int port;
    private final ExecutorService pool;
    private final EchoObject service = new EchoObject();

    public EchoMultiServer(int port, int maxThreads) {
        this.port = port;
        this.pool = Executors.newFixedThreadPool(maxThreads); // Límite de hilos
    }

    public void start() throws IOException {
        try (ServerSocket serverSocket = new ServerSocket(port)) {
            System.out.println("[SERVIDOR] Escuchando en puerto " + port);
            while (true) {
                Socket client = serverSocket.accept();
                pool.execute(new ClientTask(client, service));
            }
        } finally {
            pool.shutdown(); // Cierra el pool al terminar
        }
    }

    public static void main(String[] args) throws IOException {
        int port = (args.length > 0) ? Integer.parseInt(args[0]) : 5000;
        int maxThreads = (args.length > 1) ? Integer.parseInt(args[1]) : 50;
        new EchoMultiServer(port, maxThreads).start();
    }

    // Tarea por cliente
    private static class ClientTask implements Runnable {
        private final Socket socket;
        private final EchoObject service;

        ClientTask(Socket socket, EchoObject service) {
            this.socket = socket;
            this.service = service;
        }

        @Override
        public void run() {
            System.out.println("[SERVIDOR] Conectado: " + socket.getRemoteSocketAddress());
            try (BufferedReader in = new BufferedReader(new InputStreamReader(socket.getInputStream()));
                 PrintWriter out = new PrintWriter(new BufferedWriter(new OutputStreamWriter(socket.getOutputStream())), true)) {

                String line;
                while ((line = in.readLine()) != null) {
                    if ("quit".equalsIgnoreCase(line.trim())) {
                        out.println("bye");
                        break;
                    }
                    String response = service.process(line); // tarda ~3s
                    out.println(response); // eco al cliente
                }
            } catch (IOException e) {
                System.err.println("[SERVIDOR] Error con cliente " + socket.getRemoteSocketAddress() + ": " + e.getMessage());
            } finally {
                try { socket.close(); } catch (IOException ignored) {}
                System.out.println("[SERVIDOR] Desconectado: " + socket.getRemoteSocketAddress());
            }
        }
    }
}
