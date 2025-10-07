/*
package client;
import java.io.*;
import server.EchoObject;

public class Echo {
    
    public static void main(String[] args) 
    {
        String cadena = "";
        
        // Creamos una instancia LOCAL del objeto
        EchoObject eo = new EchoObject();
        
        System.out.println("Cliente Echo - Versión NO distribuida");
        System.out.println("Escribe mensajes:");
        
        try {  
            while(true) {
                BufferedReader in = new BufferedReader(new InputStreamReader(System.in));
                cadena = in.readLine();
                String resultado = eo.echo(cadena);
                System.out.println(resultado);
            }
        } 
        catch (Exception e) {
            System.err.println("Error: " + e.getMessage());
        }
    }
}
*/
package client;
import java.io.*;
import java.net.*;

public class Echo {
    private static EchoObjectStub ss;
    
    public static void main(String[] args) 
    {
        String cadena="";
        if (args.length<2) {
            System.out.println("Para ejecutar , hazlo en este formato: Echo <nombre o IP del Equipo> <numero de puerto>");
            System.exit(1);
        }
        
        ss = new EchoObjectStub();
        ss.setHostAndPort(args[0],Integer.parseInt(args[1]));
        
        BufferedReader stdIn = new BufferedReader(new InputStreamReader(System.in));
        PrintWriter stdOut = new PrintWriter(System.out);
        String input,output;
        
        try {  
            while(true){
                BufferedReader in = new BufferedReader(new InputStreamReader(System.in));
                cadena=in.readLine();
                System.out.println(ss.echo(cadena));
            }
        } 
        catch (IOException e) {
            System.err.println("Falla conexion de E/S con el host:"+args[0]);
        }
    }
}