// Runs the real resistor_bank_controller.ino against the fake core in Arduino.h.
//
// Each stdin line is one message from the host, handed to loop() as the next
// serial line. "@wait <ms>" instead advances the clock by that much and runs
// loop() once with nothing to read. After every line the driver prints
//     PINS <pin 4> <pin 5> ... <pin 12>
// with each pin's level, 1 for HIGH, so the test sees the relays exactly as the
// sketch left them. The sketch's own Serial output appears in between.
#include "Arduino.h"

int pinState[20];
unsigned long fakeMillis = 0;
FakeSerial Serial;

#include "resistor_bank_controller.ino"

#include <iostream>

int main() {
  setup();
  std::string line;
  while (std::getline(std::cin, line)) {
    if (line.rfind("@wait ", 0) == 0) {
      fakeMillis += std::stoul(line.substr(6));
    } else {
      fakeMillis += 10;
      Serial.input += line + "\n";
    }
    loop();
    std::printf("PINS");
    for (int pin = 4; pin <= 12; pin++) std::printf(" %d", pinState[pin]);
    std::printf("\n");
  }
  return 0;
}
