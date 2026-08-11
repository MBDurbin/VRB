#include <OneWire.h>
#include <DallasTemperature.h>

// Data wire is plugged into digital pin 2 on the Arduino
#define ONE_WIRE_BUS 2

// Setup a oneWire instance to communicate with any OneWire devices
OneWire oneWire(ONE_WIRE_BUS);

// Pass our oneWire reference to Dallas Temperature sensor 
DallasTemperature sensors(&oneWire);

// Variable to hold the number of devices found
int deviceCount = 0;

void setup(void) {
  // Start serial port
  Serial.begin(9600);
  Serial.println("Dallas Temperature Sensor Scanner");
  Serial.println("---------------------------------");

  // Start up the library
  sensors.begin();

  // Locate devices on the bus
  deviceCount = sensors.getDeviceCount();
  Serial.print("Locating devices...");
  Serial.print("Found ");
  Serial.print(deviceCount, DEC);
  Serial.println(" devices.");
  Serial.println("");
}

void loop(void) {
  // Call sensors.requestTemperatures() to issue a global temperature 
  // request to all devices on the bus
  sensors.requestTemperatures(); 
  
  // Loop through each device, print its ID and temperature
  for (int i = 0; i < deviceCount; i++) {
    DeviceAddress tempDeviceAddress;
    
    // Search the wire for address
    if (sensors.getAddress(tempDeviceAddress, i)) {
      Serial.print("Sensor ");
      Serial.print(i + 1);
      Serial.print(" | ID: ");
      printAddress(tempDeviceAddress);
      
      // Print the data
      float tempC = sensors.getTempC(tempDeviceAddress);
      float tempF = sensors.getTempF(tempDeviceAddress); // Optional: Get Fahrenheit
      
      Serial.print(" | Temp: ");
      Serial.print(tempC);
      Serial.print(" °C (");
      Serial.print(tempF);
      Serial.println(" °F)");
    } else {
      Serial.print("Found ghost device at index ");
      Serial.print(i);
      Serial.println(" but could not detect address. Check power and cabling");
    }
  }
  
  Serial.println("---------------------------------");
  delay(2000); // Wait 2 seconds before the next reading
}

// Helper function to print a device address
void printAddress(DeviceAddress deviceAddress) {
  for (uint8_t i = 0; i < 8; i++) {
    // Zero pad the address if necessary
    if (deviceAddress[i] < 16) {
      Serial.print("0");
    }
    Serial.print(deviceAddress[i], HEX);
  }
}