#include <OneWire.h>
#include <DallasTemperature.h>

// 1. Define the digital pins for your 6 buses
#define BUS_1_PIN 2
#define BUS_2_PIN 3
#define BUS_3_PIN 4
#define BUS_4_PIN 5
#define BUS_5_PIN 6
#define BUS_6_PIN 7

// 2. Setup OneWire instances for each pin
OneWire oneWire1(BUS_1_PIN);
OneWire oneWire2(BUS_2_PIN);
OneWire oneWire3(BUS_3_PIN);
OneWire oneWire4(BUS_4_PIN);
OneWire oneWire5(BUS_5_PIN);
OneWire oneWire6(BUS_6_PIN);

// 3. Pass the OneWire references to DallasTemperature
DallasTemperature sensors1(&oneWire1);
DallasTemperature sensors2(&oneWire2);
DallasTemperature sensors3(&oneWire3);
DallasTemperature sensors4(&oneWire4);
DallasTemperature sensors5(&oneWire5);
DallasTemperature sensors6(&oneWire6);

DallasTemperature* buses[] = {&sensors1, &sensors2, &sensors3, &sensors4, &sensors5, &sensors6};

// Create an array of the sensor buses to easily loop through them
const int NUM_BUSES = 6;
const int SENSORS_PER_BUS = 8;

// 4. Input your specific Sensor IDs here
DeviceAddress bus1_IDs[SENSORS_PER_BUS] = {
  {0x28, 0xDB, 0x84, 0xFB, 0x0F, 0x00, 0x00, 0x4C}, // Sensor A1
  {0x28, 0x3B, 0xFD, 0xFB, 0x0F, 0x00, 0x00, 0xFB}, // Sensor B1
  {0x28, 0xDA, 0x4A, 0xFB, 0x0F, 0x00, 0x00, 0xFB}, // Sensor C1
  {0x28, 0xE0, 0xBE, 0xFB, 0x0F, 0x00, 0x00, 0x60}, // Sensor D1
  {0x28, 0x7A, 0x9B, 0xFB, 0x0F, 0x00, 0x00, 0x34}, // Sensor A2
  {0x28, 0xEF, 0x2E, 0xFB, 0x0F, 0x00, 0x00, 0xF3}, // Sensor B2
  {0x28, 0x4F, 0x46, 0xFB, 0x0F, 0x00, 0x00, 0x80}, // Sensor C2
  {0x28, 0xFB, 0x97, 0xFB, 0x0F, 0x00, 0x00, 0xC8}  // Sensor D2
};

DeviceAddress bus2_IDs[SENSORS_PER_BUS] = {
  { 0x28, 0x97, 0x99, 0xFB, 0x0F, 0x00, 0x00, 0xD4 }, // Sensor A3
  { 0x28, 0xAC, 0xF3, 0xFA, 0x0F, 0x00, 0x00, 0xE2 }, // Sensor B3
  { 0x28, 0xBF, 0xCA, 0xFB, 0x0F, 0x00, 0x00, 0x18 }, // Sensor C3
  { 0x28, 0xA6, 0xCE, 0xFB, 0x0F, 0x00, 0x00, 0xCA }, // Sensor D3
  { 0x28, 0x5E, 0x96, 0xFB, 0x0F, 0x00, 0x00, 0xB2 }, // Sensor A4
  { 0x28, 0x69, 0xCF, 0xFB, 0x0F, 0x00, 0x00, 0xBC }, // Sensor B4
  { 0x28, 0x16, 0xCF, 0xFA, 0x0F, 0x00, 0x00, 0x8F }, // Sensor C4
  { 0x28, 0x7E, 0xE1, 0xFB, 0x0F, 0x00, 0x00, 0x38 }  // Sensor D4
};

DeviceAddress bus3_IDs[SENSORS_PER_BUS] = {
  { 0x28, 0x7E, 0x2F, 0xFB, 0x0F, 0x00, 0x00, 0xB8 }, // Sensor A5
  { 0x28, 0x9E, 0x04, 0xFC, 0x0F, 0x00, 0x00, 0x9F }, // Sensor B5
  { 0x28, 0xE4, 0x50, 0xFB, 0x0F, 0x00, 0x00, 0xC4 }, // Sensor C5
  { 0x28, 0x9F, 0xAA, 0xFB, 0x0F, 0x00, 0x00, 0xBF }, // Sensor D5
  { 0x28, 0x00, 0x4E, 0xFB, 0x0F, 0x00, 0x00, 0xEF }, // Sensor A6
  { 0x28, 0x6B, 0xAF, 0xFB, 0x0F, 0x00, 0x00, 0xC3 }, // Sensor B6
  { 0x28, 0xC2, 0x02, 0xFC, 0x0F, 0x00, 0x00, 0x50 }, // Sensor C6
  { 0x28, 0x39, 0x41, 0xFB, 0x0F, 0x00, 0x00, 0xFB }  // Sensor D6
};

DeviceAddress bus4_IDs[SENSORS_PER_BUS] = {
  { 0x28, 0x91, 0x76, 0xFB, 0x0F, 0x00, 0x00, 0xD3 }, // Sensor A7
  { 0x28, 0x35, 0xDB, 0xFB, 0x0F, 0x00, 0x00, 0x8C }, // Sensor B7
  { 0x28, 0x69, 0xD9, 0xFB, 0x0F, 0x00, 0x00, 0x5C }, // Sensor C7
  { 0x28, 0x24, 0xBC, 0xFA, 0x0F, 0x00, 0x00, 0x2F }, // Sensor D7
  { 0x28, 0xA9, 0xF6, 0xFB, 0x0F, 0x00, 0x00, 0x54 }, // Sensor A8
  { 0x28, 0xEB, 0xD3, 0xFB, 0x0F, 0x00, 0x00, 0x65 }, // Sensor B8
  { 0x28, 0x40, 0x52, 0xFB, 0x0F, 0x00, 0x00, 0xC7 }, // Sensor C8
  { 0x28, 0xC7, 0x96, 0xFB, 0x0F, 0x00, 0x00, 0x95 }  // Sensor D8
};

DeviceAddress bus5_IDs[SENSORS_PER_BUS] = {
  { 0x28, 0xF8, 0x05, 0xFC, 0x0F, 0x00, 0x00, 0x23 }, // Sensor A9
  { 0x28, 0xB8, 0xC6, 0xFB, 0x0F, 0x00, 0x00, 0xBC }, // Sensor B9
  { 0x28, 0x4D, 0x9D, 0xFA, 0x0F, 0x00, 0x00, 0x4F }, // Sensor C9
  { 0x28, 0x29, 0xC1, 0xFA, 0x0F, 0x00, 0x00, 0xE4 }, // Sensor D9
  { 0x28, 0x4B, 0x89, 0xFB, 0x0F, 0x00, 0x00, 0x11 }, // Sensor A10
  { 0x28, 0x28, 0x06, 0xFC, 0x0F, 0x00, 0x00, 0xA9 }, // Sensor B10
  { 0x28, 0xAA, 0x3E, 0xFB, 0x0F, 0x00, 0x00, 0x11 }, // Sensor C10
  { 0x28, 0x70, 0x42, 0xFB, 0x0F, 0x00, 0x00, 0x56 }  // Sensor D10
};

DeviceAddress bus6_IDs[SENSORS_PER_BUS] = {
  { 0x28, 0xD0, 0x02, 0xFB, 0x0F, 0x00, 0x00, 0xE3 }, // Sensor A11
  { 0x28, 0xEA, 0xED, 0xFA, 0x0F, 0x00, 0x00, 0xFB }, // Sensor B11
  { 0x28, 0x97, 0xC9, 0xFB, 0x0F, 0x00, 0x00, 0x41 }, // Sensor C11
  { 0x28, 0xEB, 0x06, 0xFB, 0x0F, 0x00, 0x00, 0xE9 }, // Sensor D11
  { 0x28, 0x8A, 0x8C, 0xFB, 0x0F, 0x00, 0x00, 0x6B }, // Sensor A12
  { 0x28, 0xF8, 0xD8, 0xFB, 0x0F, 0x00, 0x00, 0x17 }, // Sensor B12
  { 0x28, 0x65, 0x01, 0xFB, 0x0F, 0x00, 0x00, 0x41 }, // Sensor C12
  { 0x28, 0x04, 0x25, 0xFB, 0x0F, 0x00, 0x00, 0x52 }  // Sensor D12
};

// ================= BUS-BAR SENSORS =================
// One DS18B20 on each bus bar that joins one group of four parallel cells to
// the next: eleven on a 12S module, the bar between S1 and S2 first and the bar
// between S11 and S12 last. The cell sensors above sit on the cell tops; these
// sit on the current path itself, so a weld or a bar running hot shows here
// first.
//
// They are sent on a line of their own after the six cell buses:
//
//     B,<t1>,<t2>,...,<tN>
//
// with ERR for a sensor that cannot be read, as on the cell lines. The host
// takes the line only if it carries exactly as many readings as
// busbar_sensor_count in rig_config.json says, so the two numbers must match.
//
// TO FIT THEM:
//   1. Wire the sensors to BUSBAR_BUS_A_PIN, and to BUSBAR_BUS_B_PIN as well if
//      one bus is too long: the cell sensors are split eight to a bus to keep
//      each bus's capacitance down, and eleven on one is more than that.
//   2. Read each sensor's address with arduino/ds18b20_address_scanner and put
//      it in busbarSensors[] below, in bus-bar order, with the bus it is on.
//   3. Set BUSBAR_SENSOR_COUNT to the number fitted and flash this sketch.
//   4. Set "busbar_sensor_count" in rig_config.json to the same number and
//      restart the rig.
// Do 3 and 4 together. With the count set here and not there the host ignores
// the line; with it set there and not here the rig faults NO BUSBAR DATA as
// soon as it is armed.
//
// 0 leaves all of this out of the build: no line is sent and nothing changes.
#ifndef BUSBAR_SENSOR_COUNT
#define BUSBAR_SENSOR_COUNT 0
#endif

#if BUSBAR_SENSOR_COUNT > 0
#define BUSBAR_BUS_A_PIN 8
#define BUSBAR_BUS_B_PIN 9

OneWire oneWireBusbarA(BUSBAR_BUS_A_PIN);
OneWire oneWireBusbarB(BUSBAR_BUS_B_PIN);
DallasTemperature busbarBusA(&oneWireBusbarA);
DallasTemperature busbarBusB(&oneWireBusbarB);
DallasTemperature* busbarBuses[] = {&busbarBusA, &busbarBusB};
const int NUM_BUSBAR_BUSES = 2;

struct BusbarSensor {
  uint8_t bus;            // 0: the bus on BUSBAR_BUS_A_PIN. 1: BUSBAR_BUS_B_PIN.
  DeviceAddress address;  // from arduino/ds18b20_address_scanner
};

// In bus-bar order. Every address here is a placeholder: a row left as it is
// reads ERR, and the rig names that bus bar as a failed sensor once armed.
// A row short of BUSBAR_SENSOR_COUNT reads ERR too; a row too many will not
// compile.
BusbarSensor busbarSensors[BUSBAR_SENSOR_COUNT] = {
  {0, {0x28, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}}, // Bus bar 1:  between S1 and S2
  {0, {0x28, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}}, // Bus bar 2:  between S2 and S3
  {0, {0x28, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}}, // Bus bar 3:  between S3 and S4
  {0, {0x28, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}}, // Bus bar 4:  between S4 and S5
  {0, {0x28, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}}, // Bus bar 5:  between S5 and S6
  {0, {0x28, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}}, // Bus bar 6:  between S6 and S7
  {1, {0x28, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}}, // Bus bar 7:  between S7 and S8
  {1, {0x28, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}}, // Bus bar 8:  between S8 and S9
  {1, {0x28, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}}, // Bus bar 9:  between S9 and S10
  {1, {0x28, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}}, // Bus bar 10: between S10 and S11
  {1, {0x28, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}}, // Bus bar 11: between S11 and S12
};
#endif

unsigned long lastRequestTime = 0;
int currentState = 0;
const int CONVERSION_DELAY = 100; // 100ms wait for 9-bit resolution

// We will use an array to make looping through the IDs easy
DeviceAddress* all_bus_IDs[] = {bus1_IDs, bus2_IDs, bus3_IDs, bus4_IDs, bus5_IDs, bus6_IDs};

void setup() {
  Serial.begin(115200); // Crucial: Fast baud rate so serial sending doesn't cause lag

  for (int i = 0; i < NUM_BUSES; i++) {
    buses[i]->begin();
    buses[i]->setResolution(9); // Drop to 9-bit for maximum speed (94ms conversion)
    buses[i]->setWaitForConversion(false);
  }

#if BUSBAR_SENSOR_COUNT > 0
  for (int i = 0; i < NUM_BUSBAR_BUSES; i++) {
    busbarBuses[i]->begin();
    busbarBuses[i]->setResolution(9);
    busbarBuses[i]->setWaitForConversion(false);
  }
#endif
}

void loop() {
  // --- NEW: Hardware Manager Auto-Discovery Handshake ---
  if (Serial.available() > 0) {
    String incomingData = Serial.readStringUntil('\n');
    incomingData.trim();
    if (incomingData == "?WHOAMI") {
      Serial.println("TEMP_SENSOR");
    }
  }
  // ------------------------------------------------------

  unsigned long currentMillis = millis();

  switch (currentState) {
    case 0: // STATE 0: TRIGGER MEASUREMENT
      for (int i = 0; i < NUM_BUSES; i++) {
        buses[i]->requestTemperatures();
      }
#if BUSBAR_SENSOR_COUNT > 0
      for (int i = 0; i < NUM_BUSBAR_BUSES; i++) {
        busbarBuses[i]->requestTemperatures();
      }
#endif
      lastRequestTime = currentMillis;
      currentState = 1; // Move to the waiting state
      break;

    case 1: // STATE 1: WAIT FOR SENSORS
      // If 100ms has passed, the sensors are done calculating
      if (currentMillis - lastRequestTime >= CONVERSION_DELAY) {
        currentState = 2; // Move to reading Bus 1
      }
      // If 100ms hasn't passed, the Arduino can run other code here!
      break;

    // STATES 2 through 7: READ AND TRANSMIT EACH BUS
    case 2:
    case 3:
    case 4:
    case 5:
    case 6:
    case 7: {
      int busIndex = currentState - 2; // Converts state 2-7 into bus index 0-5
     
      // Print the Bus Number first
      Serial.print(busIndex + 1);
     
      // Read and print all 8 sensors on this bus
      for (int i = 0; i < SENSORS_PER_BUS; i++) {
        float tempC = buses[busIndex]->getTempC(all_bus_IDs[busIndex][i]);
        Serial.print(","); // Comma separator for Python to split easily
        if (tempC <= -100.0) { // Catch disconnected sensor errors
          Serial.print("ERR");
        } else {
          Serial.print(tempC, 1); // Print with 1 decimal place to save serial bandwidth
        }
      }
      Serial.println(); // End the line, Python will read this as a complete packet
     
      currentState++; // Move to the next bus (or back to start)

#if BUSBAR_SENSOR_COUNT == 0
      if (currentState > 7) {
        currentState = 0; // All buses read, restart the cycle
      }
#endif
      break;
    }

#if BUSBAR_SENSOR_COUNT > 0
    // STATE 8: READ AND TRANSMIT THE BUS BARS, then restart the cycle
    case 8: {
      Serial.print("B"); // Marks the bus-bar line; the cell lines start with a number
      for (int i = 0; i < BUSBAR_SENSOR_COUNT; i++) {
        float tempC = -127.0; // what the library returns for a sensor it cannot read
        if (busbarSensors[i].bus < NUM_BUSBAR_BUSES) {
          tempC = busbarBuses[busbarSensors[i].bus]->getTempC(busbarSensors[i].address);
        }
        Serial.print(",");
        if (tempC <= -100.0) {
          Serial.print("ERR");
        } else {
          Serial.print(tempC, 1);
        }
      }
      Serial.println();
      currentState = 0;
      break;
    }
#endif
  }
}