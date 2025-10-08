public class EchoObject {
    // Simula un servicio que tarda 3 segundos para que se note la concurrencia
    public String process(String input) {
        try { Thread.sleep(3000); } 
        catch (InterruptedException e) { Thread.currentThread().interrupt(); }
        return "ECHO> " + input;
    }
}
