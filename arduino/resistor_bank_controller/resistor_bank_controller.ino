// --- Function Prototypes ---
void turnONAllRESISTORS();
bool isBinary(String str);
bool isAllZeros(String str);
void updateOtherRelays(String binaryStr);

// String to hold the current state
String currentState = "";
String MainRelay = "Open";
bool noSignalAnnounced = false;

// ================= HARDWARE CONFIGURATION =================
const int RELAY_MAIN_PIN = 4;       // Main relay d4
const int BANK_1_RELAY   = 5;        // 0.25 ohm relay on Digital
const int otherRelayPins[] = {12, 11, 10, 9, 8, 7, 6}; // Relays
const int numOtherRelays = sizeof(otherRelayPins) / sizeof(otherRelayPins[0]);

const int RELAY_OPEN  = LOW;  // LED OFF
const int RELAY_CLOSE = HIGH;  // LED ON

// ================= CLOCK VARIABLES =================
unsigned long lastConnectionTime = 0;  
const long TIMEOUT_LIMIT = 2000;        

// ================= SETUP =================
void setup() {
  pinMode(RELAY_MAIN_PIN, OUTPUT);
  digitalWrite(RELAY_MAIN_PIN, RELAY_OPEN);
  MainRelay = "Open";

  pinMode(BANK_1_RELAY, OUTPUT);
  digitalWrite(BANK_1_RELAY, RELAY_OPEN); //turn on the 0.25 ohm resistor

  for (int i = 0; i < numOtherRelays; i++) {
    pinMode(otherRelayPins[i], OUTPUT);
    digitalWrite(otherRelayPins[i], RELAY_OPEN); //turn on all other resistors
  }

  Serial.begin(9600);
  Serial.println("Arduino Ready. Waiting for Binary Signal...");
}

// ================= MAIN LOOP =================
void loop() {

  // 1. WATCHDOG CHECK
  if (millis() - lastConnectionTime > TIMEOUT_LIMIT) {
      turnONAllRESISTORS();
  }

// 2. CHECK FOR INCOMING DATA
  if (Serial.available() > 0) {

    String incomingData = Serial.readStringUntil('\n');
    incomingData.trim();
    lastConnectionTime = millis();
    noSignalAnnounced = false;

    // 3. FILTERS & HANDSHAKES (Move these UP!)
    if (incomingData == "?WHOAMI") {
      Serial.println("RESISTOR_CTRL");
      return; 
    }

    if (incomingData == "alive") {
      return;
    }

    // Now it is safe to close the Main Relay, because we know the 
    // incoming command is actually meant for the resistor bank.
    if (MainRelay == "Open"){
      digitalWrite(RELAY_MAIN_PIN, RELAY_CLOSE);
      MainRelay = "Close";
    }

    // Fixed: Matches Python's uppercase "KILL" command
    if (incomingData == "KILL" || incomingData == "kill") {
      turnONAllRESISTORS();
      return; // Stop processing so it doesn't hit the binary check
    }

    // Is it binary?
    if (!isBinary(incomingData)) {
       Serial.println("Error: Not a binary signal.");
       return;
    }

    // SAFETY INTERLOCK
    if (isAllZeros(incomingData)) {
       Serial.println("SAFETY ACTION: All-Zero detected. Adjusting...");
       incomingData.setCharAt(incomingData.length() - 1, '1');
    }

    // State change check
    if (incomingData == currentState) {
      return;
    }

    Serial.print("New State Received: ");
    Serial.println(incomingData);

    // OPEN 0.25 ohm relay (Safety Step)
    digitalWrite(BANK_1_RELAY, RELAY_OPEN);
    Serial.println("Action: 0.25 relay opened");
    
    // Fixed: 50ms provides enough time for mechanical relay clearance and debouncing
    // without stalling the 1Hz Python physics loop.
    delay(50); 

    // Switch other relays
    updateOtherRelays(incomingData);
    Serial.println("Action: All Other Relays Changed");
    
    delay(50); 

    // Check last bit logic
    char lastChar = incomingData.charAt(incomingData.length() - 1);

    if (lastChar == '1') {
      digitalWrite(BANK_1_RELAY, RELAY_OPEN);
      Serial.println("Action: 0.25 ohm Relay Kept OPEN.");
    }
    else {
      digitalWrite(BANK_1_RELAY, RELAY_CLOSE);
      Serial.println("Action: 0.25 ohm Relay Kept CLOSED.");
    }

    currentState = incomingData;
  }
}

// ================= HELPER FUNCTIONS =================

void turnONAllRESISTORS() {
  digitalWrite(BANK_1_RELAY, RELAY_OPEN);
  for (int i = 0; i < numOtherRelays; i++) {
    digitalWrite(otherRelayPins[i], RELAY_OPEN);
  }
  digitalWrite(RELAY_MAIN_PIN, RELAY_OPEN);

  currentState = "";
  MainRelay = "Open";

  if (!noSignalAnnounced) {
    Serial.println("No Signal: System Reset / All Off");
    noSignalAnnounced = true;
  }
}

bool isBinary(String str) {
  if (str.length() == 0) return false;
  for (unsigned int i = 0; i < str.length(); i++) {
    if (str.charAt(i) != '0' && str.charAt(i) != '1') {
      return false;
    }
  }
  return true;
}

bool isAllZeros(String str) {
  for (unsigned int i = 0; i < str.length(); i++) {
    if (str.charAt(i) != '0') {
      return false;
    }
  }
  return true;
}

void updateOtherRelays(String binaryStr) {
  int limit = min((int)binaryStr.length(), numOtherRelays);
  for (int i = 0; i < limit; i++) {
    char bit = binaryStr.charAt(i);
    digitalWrite(otherRelayPins[i], (bit == '1') ? RELAY_OPEN : RELAY_CLOSE);
  }
}