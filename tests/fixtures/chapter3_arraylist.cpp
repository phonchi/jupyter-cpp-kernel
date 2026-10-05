#include <iostream>
#include "pythonds3/cppds/arraylist.hpp"   // the complete class of this section
using namespace std;
void print(const ArrayList& a) {
    for (int i = 0; i < a.size(); i++) cout << a[i] << " ";
    cout << "(size " << a.size() << ", capacity " << a.capacity() << ")" << endl;
}
int main() {
    ArrayList myArray;
    myArray.push_back(31); myArray.push_back(77);
    myArray.push_back(17); myArray.push_back(93);
    print(myArray);
    cout << myArray[3] << " " << myArray.size() << endl;

    myArray.insert(3, 20);
    myArray.erase(0);          // remove 31, which sits at index 0
    print(myArray);

    myArray.erase(2);
    myArray[0] = 50;
    print(myArray);
    return 0;
}